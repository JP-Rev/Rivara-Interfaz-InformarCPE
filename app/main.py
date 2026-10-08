"""Interfaz para informar CPE a Visec.

Se pegan (o se suben) los números de CTG, la app consulta cada CPE en el WS de
ARCA y arma el XLSX con la plantilla de Visec.
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
from contextlib import asynccontextmanager
import os
import re
import time
import unicodedata
import uuid
import zipfile
from datetime import datetime
from pathlib import Path

import jwt
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, config
from . import albor
from .planillas import filas as filas_planilla
from . import certificados
from .correo import ErrorCorreo, enviar as enviar_mail, problemas_correo
from .planillas import a_texto as planilla_a_texto, es_planilla
from .visec import Fila, armar_fila, escribir
from .wscpe import consultar

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
log = logging.getLogger(__name__)

COOKIE_SESION = "rivara_he"  # la misma del portal: no se pide login de nuevo
MAX_CTG = int(os.getenv("MAX_CTG", "200"))
SALIDA_DIR = Path(os.getenv("SALIDA_DIR", "/data/salida"))
RETENCION_SALIDA_DIAS = int(os.getenv("RETENCION_SALIDA_DIAS", "30"))
RETENCION_RESPUESTAS_DIAS = int(os.getenv("RETENCION_RESPUESTAS_DIAS", "365"))


def purgar(carpeta: Path, patron: str, dias: int) -> int:
    """Borra los archivos mas viejos que `dias`. Devuelve cuantos borro.

    Los XLSX y las respuestas de ARCA viven en /storage, que es el disco que se
    respalda (contrato §7), asi que no pueden acumularse para siempre.
    """
    if dias <= 0 or not carpeta.is_dir():
        return 0
    limite = time.time() - dias * 86400
    borrados = 0
    for archivo in carpeta.glob(patron):
        try:
            if archivo.is_file() and archivo.stat().st_mtime < limite:
                archivo.unlink()
                borrados += 1
        except OSError as error:  # permisos, o lo borro otro proceso
            log.warning("No se pudo borrar %s: %s", archivo, error)
    if borrados:
        log.info("Purga: %s archivos de mas de %s dias en %s", borrados, dias, carpeta)
    return borrados


def purgar_todo() -> None:
    purgar(SALIDA_DIR, "visec_cpe_*.xlsx", RETENCION_SALIDA_DIAS)
    purgar(SALIDA_DIR, "albor_cosecha_*.xlsx", RETENCION_SALIDA_DIAS)
    purgar(SALIDA_DIR, "albor_cp_*.xlsx", RETENCION_SALIDA_DIAS)  # de la versión anterior
    purgar(config.cache_dir / "respuestas", "cpe_*.json", RETENCION_RESPUESTAS_DIAS)


@asynccontextmanager
async def ciclo_de_vida(_: FastAPI):
    problemas = config.validar()
    if problemas:
        for problema in problemas:
            log.warning("Configuracion: %s", problema)
    else:
        log.info("Configuracion completa (%s, CUIT %s)", config.entorno, ", ".join(config.cuits))
    purgar_todo()
    tarea = asyncio.create_task(_vigilar_certificados())
    yield
    tarea.cancel()


async def _vigilar_certificados() -> None:
    """Revisa los vencimientos al arrancar y cada CERT_CHEQUEO_HORAS.

    No hay que esperar a que alguien abra la app: el mail sale solo. Que no se
    repita lo resuelve chequear_y_avisar, que recuerda los umbrales avisados.
    """
    while True:
        try:
            enviados = await asyncio.to_thread(certificados.chequear_y_avisar)
            if enviados:
                log.info("Avisos de vencimiento enviados: %s", "; ".join(enviados))
        except Exception:  # un error aca no puede tirar abajo la app
            log.exception("Fallo el chequeo de vencimiento de certificados")
        await asyncio.sleep(max(config.cert_chequeo_horas, 1) * 3600)


app = FastAPI(title="Informar CPE a Visec", lifespan=ciclo_de_vida)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
plantillas = Jinja2Templates(directory=str(BASE_DIR / "templates"))


def _version_estaticos() -> str:
    """Huella de los CSS, para pedirlos como app.css?v=...: sin esto el
    navegador sigue usando el CSS de la version anterior despues de un deploy
    y la pantalla se ve rota (HTML nuevo con estilos viejos)."""
    import hashlib

    huella = hashlib.sha256()
    for ruta in sorted((BASE_DIR / "static").glob("*.css")):
        huella.update(ruta.read_bytes())
    return huella.hexdigest()[:10]


plantillas.env.globals["version_estaticos"] = _version_estaticos()


# ---------------------------------------------------------------------------
# Sesión compartida con el portal
# ---------------------------------------------------------------------------

def usuario_de(request: Request) -> dict | None:
    if config.auth_desactivada:
        return {"name": "Desarrollo", "email": ""}
    token = request.cookies.get(COOKIE_SESION)
    if not token or not config.jwt_secret:
        return None
    try:
        return jwt.decode(token, config.jwt_secret, algorithms=["HS256"])
    except jwt.PyJWTError:
        return None


def url_portal(request: Request) -> str:
    """La URL del portal, armada con el host por el que entró el navegador.

    Hardcodearla rompe la mitad de los accesos: quien entra por IP no resuelve
    `admin`, y quien entra por nombre no comparte la cookie con la IP.
    """
    if config.portal_url:
        return config.portal_url
    host = request.url.hostname
    return f"{request.url.scheme}://{host}:{config.portal_puerto}/" if host else ""


# ---------------------------------------------------------------------------
# Versiones operario / admin
# ---------------------------------------------------------------------------

# La version elegida por un admin. Es de esta app, no del portal: el rol viene
# del portal (payload "role" del JWT), la preferencia se guarda aca.
COOKIE_MODO = "informarcpe_modo"
MODOS = ("operario", "admin")
SCRIPTS_DIR = BASE_DIR.parent / "scripts"


def es_admin(usuario: dict) -> bool:
    """ADMIN en el portal. En desarrollo, sin sesion, se trata como admin."""
    return config.auth_desactivada or usuario.get("role") == "ADMIN"


def modo_de(request: Request, usuario: dict) -> str | None:
    """'operario', 'admin', o None si un admin todavia no eligio."""
    if not es_admin(usuario):
        return "operario"
    modo = request.cookies.get(COOKIE_MODO)
    return modo if modo in MODOS else None


def _sin_sesion(request: Request) -> RedirectResponse | HTTPException:
    """Al portal si se puede resolver; si no, un 401 sin inventar un login propio."""
    destino = url_portal(request)
    if destino:
        return RedirectResponse(destino, status_code=303)
    return HTTPException(status_code=401, detail="Sesión no válida. Entrá desde el portal.")


# ---------------------------------------------------------------------------
# Entrada: lista de CTG pegada o archivo del script
# ---------------------------------------------------------------------------

ALIAS = {
    "ctg": "ctg",
    "nro_ctg": "ctg",
    "numero_ctg": "ctg",
    "fecha_movimiento": "fecha_movimiento",
    "fecha_hora_movimiento": "fecha_movimiento",
    "fecha_arribo": "fecha_movimiento",
    "peso_ingreso_stock": "peso_ingreso_stock",
    "peso_neto": "peso_ingreso_stock",
    "kilos_estimados": "kilos_estimados",
    "renspa": "renspa",
    "numero_renspa": "renspa",
}


def _normalizar(nombre: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", sin_tildes.strip().lower()).strip("_")


# El CTG es un número de 11 dígitos. Validarlo evita inventar CTG: un archivo
# en ancho fijo sin separadores, pasado por "sacar todo lo que no es dígito",
# da un número de 80 cifras que ARCA rechaza o, peor, que pasa por bueno.
CTG = re.compile(r"\d{11}")

# Columnas del TXT que exporta el editor SQL del ERP con la consulta de
# app/consultas/ingresos_erp.sql: (campo, desde, hasta). El editor exporta en
# ancho fijo, sin encabezado ni separadores, y cada columna ocupa su tamaño
# DECLARADO, no el largo del dato: TO_CHAR de un NUMBER mide 40, ROUND() 38, el
# formato de fecha 19. Por eso las posiciones no cambian entre corridas, pero si
# cambian si se toca la consulta. Si se la modifica, hay que medir de nuevo.
ANCHO_FIJO_ERP = (
    ("ctg", 0, 14),
    ("numero_cpe", 14, 62),
    ("fecha_movimiento", 62, 81),
    ("peso_ingreso_stock", 81, 119),
    ("kilos_estimados", 119, 157),
    ("renspa", 157, 174),
)
FECHA_ERP = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")


def _es_ancho_fijo_erp(linea: str) -> bool:
    """La linea tiene la forma del TXT de la consulta del ERP."""
    return (
        len(linea) >= 174
        and CTG.fullmatch(linea[0:14].strip()) is not None
        and FECHA_ERP.fullmatch(linea[62:81]) is not None
    )


def _leer_ancho_fijo_erp(linea: str) -> dict[str, str]:
    return {campo: linea[desde:hasta].strip() for campo, desde, hasta in ANCHO_FIJO_ERP}


def parsear_entrada(texto: str) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """Devuelve [(ctg, datos_propios)] y los avisos del parseo.

    Acepta:
    - el TXT que exporta el editor SQL del ERP con la consulta de
      app/consultas/ingresos_erp.sql: ancho fijo, se lee por posición;
    - un archivo con encabezado y columnas separadas por `;`, tabulación o
      coma: el CSV de `scripts/ctg_soja.py` o la grilla copiada con Ctrl+C.
      Las columnas se reconocen por el nombre;
    - una lista de CTG, uno por línea;
    - cualquier otro archivo sin separadores: de ese solo se toma el CTG del
      principio de cada línea, con un aviso.
    """
    lineas = [l for l in texto.splitlines() if l.strip()]
    if not lineas:
        return [], []

    avisos: list[str] = []
    primera = lineas[0]

    # TXT del editor del ERP: se lee por posicion. Va primero porque la fecha
    # trae ':' y el RENSPA '/', pero ninguna de las dos es separador.
    datos = lineas
    if not CTG.match(primera.strip()) and "CTG" in primera.upper():
        datos = lineas[1:]  # encabezado, si el editor lo exporta
    if datos and all(_es_ancho_fijo_erp(l) for l in datos):
        return _sin_repetidos([_leer_ancho_fijo_erp(l) for l in datos], avisos)

    delimitador = next((d for d in (";", "\t", ",") if d in primera), None)
    if delimitador:
        filas = list(csv.reader(lineas, delimiter=delimitador))
    else:
        filas = [l.split() for l in lineas]
        if any(len(f) > 1 for f in filas):
            avisos.append(
                "El archivo no tiene la forma de la consulta del ERP: se tomaron solo los CTG, "
                "y la fecha del movimiento, los pesos y el RENSPA salen de ARCA o quedan vacíos. "
                "Usá la consulta que se descarga desde esta pantalla, sin cambiarle las columnas."
            )

    columnas: dict[str, int] = {}
    if delimitador and filas and not CTG.fullmatch(filas[0][0].strip()):
        for indice, nombre in enumerate(filas[0]):
            clave = ALIAS.get(_normalizar(nombre))
            if clave and clave not in columnas:
                columnas[clave] = indice
        filas = filas[1:]
    if not delimitador:
        columnas = {}
    columnas.setdefault("ctg", 0)

    registros = []
    for fila in filas:
        if not fila:
            continue
        registro = {
            clave: fila[indice].strip()
            for clave, indice in columnas.items()
            if indice < len(fila) and fila[indice].strip()
        }
        registros.append(registro)
    return _sin_repetidos(registros, avisos)


def _sin_repetidos(
    registros: list[dict[str, str]], avisos: list[str]
) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """Valida el CTG de cada registro y descarta los repetidos."""
    resultado: list[tuple[str, dict[str, str]]] = []
    vistos: set[str] = set()
    for numero, registro in enumerate(registros, start=1):
        ctg = registro.pop("ctg", "")
        if not CTG.fullmatch(ctg):
            avisos.append(f"Línea {numero}: '{ctg[:30]}' no es un CTG (tienen 11 dígitos)")
            continue
        if ctg in vistos:
            avisos.append(f"CTG {ctg}: repetido, se informa una sola vez")
            continue
        vistos.add(ctg)
        resultado.append((ctg, {k: v for k, v in registro.items() if v}))
    return resultado, avisos


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

# El .env guarda un identificador sin tildes; en pantalla se lee en castellano.
ETIQUETA_ENTORNO = {"produccion": "Producción", "homologacion": "Homologación"}


def _contexto(request: Request, usuario: dict, **extra) -> dict:
    return {
        "request": request,
        "usuario": usuario,
        "entorno": ETIQUETA_ENTORNO.get(config.entorno, config.entorno),
        "es_produccion": config.entorno == "produccion",
        "problemas": config.validar(),
        "max_ctg": MAX_CTG,
        "portal_url": url_portal(request),
        "es_admin": es_admin(usuario),
        "certs_alerta": [e for e in certificados.estados() if e.nivel != "ok"] if es_admin(usuario) else [],
        # None mientras un admin no eligio: la barra no muestra "Versión ...".
        "modo": modo_de(request, usuario),
        **extra,
    }


@app.get("/", response_class=HTMLResponse)
def inicio(request: Request):
    usuario = usuario_de(request)
    if not usuario:
        respuesta = _sin_sesion(request)
        if isinstance(respuesta, HTTPException):
            raise respuesta
        return respuesta
    if modo_de(request, usuario) is None:
        return plantillas.TemplateResponse(request, "elegir.html", _contexto(request, usuario))
    return plantillas.TemplateResponse(request, "index.html", _contexto(request, usuario))


@app.post("/generar", response_class=HTMLResponse)
async def generar(
    request: Request,
    ctgs: str = Form(""),
    archivo: UploadFile | None = File(None),
):
    usuario = usuario_de(request)
    if not usuario:
        respuesta = _sin_sesion(request)
        if isinstance(respuesta, HTTPException):
            raise respuesta
        return respuesta

    texto = ctgs or ""
    if archivo and archivo.filename:
        contenido = await archivo.read()
        if es_planilla(contenido):
            try:
                texto = planilla_a_texto(contenido)
            except Exception as error:  # planilla dañada o de un formato raro
                log.warning("No se pudo leer la planilla %s: %s", archivo.filename, error)
                return plantillas.TemplateResponse(
                    request, "index.html",
                    _contexto(request, usuario, error=f"No se pudo leer la planilla: {error}"),
                    status_code=400,
                )
        else:
            for codificacion in ("utf-8-sig", "latin-1"):
                try:
                    texto = contenido.decode(codificacion)
                    break
                except UnicodeDecodeError:
                    continue

    entradas, avisos_entrada = parsear_entrada(texto)
    if not entradas:
        return plantillas.TemplateResponse(
            request, "index.html",
            _contexto(request, usuario, error="No se encontró ningún número de CTG en lo que cargaste.",
                      ctgs=texto),
            status_code=400,
        )
    if len(entradas) > MAX_CTG:
        return plantillas.TemplateResponse(
            request, "index.html",
            _contexto(request, usuario,
                      error=f"Cargaste {len(entradas)} CTG y el máximo por tanda es {MAX_CTG}. "
                            "Dividilo en partes o subí MAX_CTG en el .env.",
                      ctgs=texto),
            status_code=400,
        )

    log.info("Usuario %s consulta %s CTG", usuario.get("email") or usuario.get("name"), len(entradas))
    filas: list[Fila] = []
    fallidos: list[dict[str, str]] = []
    for ctg, propios in entradas:
        consulta = consultar(ctg)
        if consulta.ok:
            filas.append(armar_fila(consulta, propios))
        else:
            fallidos.append({"ctg": ctg, "detalle": " | ".join(consulta.errores) or "Sin detalle"})

    purgar_todo()

    nombre = ""
    if filas:
        nombre = f"visec_cpe_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.xlsx"
        escribir(filas, SALIDA_DIR / nombre)

    return plantillas.TemplateResponse(
        request, "resultado.html",
        _contexto(request, usuario, filas=filas, fallidos=fallidos,
                  avisos_entrada=avisos_entrada, archivo=nombre),
    )


CONSULTA_INGRESOS = BASE_DIR / "consultas" / "ingresos_erp.sql"


@app.get("/consulta/ingresos_erp.sql")
def consulta_ingresos(request: Request):
    """La consulta para el editor SQL del ERP, para que la descarguen los operarios."""
    if not usuario_de(request):
        raise HTTPException(status_code=401, detail="Sesión no válida")
    return FileResponse(CONSULTA_INGRESOS, media_type="text/plain; charset=utf-8",
                        filename="consulta_ingresos_visec.sql")


@app.get("/modo/{modo}")
def elegir_modo(request: Request, modo: str):
    """Guarda la version elegida. 'elegir' borra la eleccion y vuelve a preguntar."""
    usuario = usuario_de(request)
    if not usuario:
        raise HTTPException(status_code=401, detail="Sesión no válida")
    if not es_admin(usuario):
        raise HTTPException(status_code=403, detail="Solo los administradores eligen versión")
    respuesta = RedirectResponse("/", status_code=303)
    if modo == "elegir":
        respuesta.delete_cookie(COOKIE_MODO, path="/")
    elif modo in MODOS:
        respuesta.set_cookie(COOKIE_MODO, modo, max_age=365 * 24 * 3600,
                             httponly=True, samesite="lax", path="/")
    else:
        raise HTTPException(status_code=404, detail="Versión desconocida")
    return respuesta


@app.get("/script/ctg_soja.zip")
def descargar_script(request: Request):
    """El script con SQL*Plus y su .bat, para los admins."""
    usuario = usuario_de(request)
    if not usuario:
        raise HTTPException(status_code=401, detail="Sesión no válida")
    if not es_admin(usuario):
        raise HTTPException(status_code=403, detail="Solo para administradores")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zip_:
        zip_.write(SCRIPTS_DIR / "ctg_soja.py", "ctg_soja.py")
        # El .bat va con fin de linea de Windows: cmd.exe se confunde con LF.
        bat = (SCRIPTS_DIR / "Generar CTG.bat").read_text(encoding="utf-8")
        zip_.writestr("Generar CTG.bat", bat.replace("\r\n", "\n").replace("\n", "\r\n"))
    return Response(buffer.getvalue(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="ctg_soja.zip"'})


def _solo_admin(request: Request) -> dict:
    usuario = usuario_de(request)
    if not usuario:
        raise HTTPException(status_code=401, detail="Sesión no válida. Entrá desde el portal.")
    if not es_admin(usuario):
        raise HTTPException(status_code=403, detail="Solo para administradores")
    return usuario


def _pagina_certificados(request: Request, usuario: dict, status_code: int = 200, **extra):
    return plantillas.TemplateResponse(
        request, "certificados.html",
        _contexto(request, usuario, seccion="certificados", estados=certificados.estados(),
                  correo_problemas=problemas_correo(), correo_para=", ".join(config.smtp_para),
                  avisos_dias=config.cert_aviso_dias, app_url=config.app_url, **extra),
        status_code=status_code,
    )


@app.get("/certificados", response_class=HTMLResponse)
def ver_certificados(request: Request):
    """Estado de los certificados de ARCA y reemplazo arrastrando el archivo."""
    try:
        usuario = _solo_admin(request)
    except HTTPException as error:
        if error.status_code == 401:
            respuesta = _sin_sesion(request)
            if isinstance(respuesta, RedirectResponse):
                return respuesta
        raise
    return _pagina_certificados(request, usuario)


@app.post("/certificados/{cuit}", response_class=HTMLResponse)
async def cargar_certificado(
    request: Request,
    cuit: str,
    crt: UploadFile = File(...),
    key: UploadFile | None = File(None),
):
    usuario = _solo_admin(request)
    if cuit not in config.cuits:
        raise HTTPException(status_code=404, detail="No hay un certificado configurado para ese CUIT")
    datos_crt = await crt.read()
    datos_key = await key.read() if key and key.filename else None
    try:
        nuevo = certificados.reemplazar(cuit, datos_crt, datos_key)
    except certificados.ErrorCertificado as error:
        return _pagina_certificados(request, usuario, status_code=400, error=str(error), error_cuit=cuit)
    log.info("Usuario %s reemplazó el certificado del CUIT %s", usuario.get("email") or usuario.get("name"), cuit)
    return _pagina_certificados(
        request, usuario,
        ok=f"Certificado del CUIT {cuit} reemplazado. Vence el {nuevo.vence:%d/%m/%Y}. "
           "Los anteriores quedaron en certs/respaldo.",
    )


@app.post("/certificados-prueba-mail", response_class=HTMLResponse)
def mail_de_prueba(request: Request):
    """Manda un mail de prueba para verificar la configuración SMTP."""
    usuario = _solo_admin(request)
    link = certificados.link_certificados()
    try:
        enviar_mail(
            "Prueba de correo — Informar CPE a Visec",
            "Si te llegó este mail, los avisos de vencimiento de los certificados de ARCA van a llegar."
            + (f"\n\nPágina de certificados: {link}" if link else ""),
            "<p>Si te llegó este mail, los avisos de vencimiento de los certificados de ARCA van a llegar.</p>"
            + (f'<p><a href="{link}">Página de certificados</a></p>' if link else ""),
        )
    except ErrorCorreo as error:
        return _pagina_certificados(request, usuario, status_code=400, error=str(error))
    return _pagina_certificados(request, usuario, ok=f"Mail de prueba enviado a {', '.join(config.smtp_para)}.")


# ---------------------------------------------------------------------------
# SoftCereal -> Albor
# ---------------------------------------------------------------------------

def _con_sesion(request: Request) -> dict | RedirectResponse:
    usuario = usuario_de(request)
    if usuario:
        return usuario
    respuesta = _sin_sesion(request)
    if isinstance(respuesta, HTTPException):
        raise respuesta
    return respuesta


def _pagina_albor(request: Request, usuario: dict, status_code: int = 200, **extra):
    return plantillas.TemplateResponse(
        request, "albor.html",
        _contexto(request, usuario, seccion="albor", refs=albor.referencias(),
                  equivalencias=albor.equivalencias(), **extra),
        status_code=status_code,
    )


@app.get("/albor", response_class=HTMLResponse)
def ver_albor(request: Request):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    return _pagina_albor(request, usuario)


@app.post("/albor/plantilla", response_class=HTMLResponse)
async def subir_plantilla_albor(request: Request, archivo: UploadFile = File(...)):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    contenido = await archivo.read()
    if not contenido.startswith(b"PK\x03\x04"):
        return _pagina_albor(request, usuario, 400, error_plantilla="La plantilla de Albor tiene que ser un .xlsx.")
    try:
        refs = await asyncio.to_thread(albor.guardar_plantilla, contenido)
    except albor.ErrorArchivo as e:
        return _pagina_albor(request, usuario, 400, error_plantilla=str(e))
    except Exception as e:
        log.warning("No se pudo leer la plantilla de Albor %s: %s", archivo.filename, e)
        return _pagina_albor(request, usuario, 400, error_plantilla=f"No se pudo leer la plantilla: {e}")
    log.info("Usuario %s cargó la plantilla de Albor %s", usuario.get("email") or usuario.get("name"), archivo.filename)
    campanas = ", ".join(albor.codigo(c) for c in refs["listas"]["campana"]) or "ninguna"
    return _pagina_albor(request, usuario, ok_plantilla=f"Plantilla cargada. Campañas en sus Referencias: {campanas}.")


@app.post("/albor", response_class=HTMLResponse)
async def generar_albor(request: Request, archivo: UploadFile = File(...)):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    refs = albor.referencias()
    if not refs:
        return _pagina_albor(request, usuario, 400, error="Primero cargá la plantilla de importación de Albor.")
    contenido = await archivo.read()
    if not es_planilla(contenido):
        return _pagina_albor(request, usuario, 400, error="El archivo no es un Excel. Subí la exportación de SoftCereal en .xlsx o .xls.")
    try:
        ingresos = albor.leer_softcereal(filas_planilla(contenido))
    except albor.ErrorArchivo as e:
        return _pagina_albor(request, usuario, 400, error=str(e))
    except Exception as e:  # planilla dañada o de un formato raro
        log.warning("No se pudo leer %s: %s", archivo.filename, e)
        return _pagina_albor(request, usuario, 400, error=f"No se pudo leer la planilla: {e}")
    tanda = albor.guardar_tanda(ingresos, archivo.filename or "")
    return _seguir_tanda(request, usuario, tanda)


def _seguir_tanda(request: Request, usuario: dict, tanda: str):
    """Si faltan equivalencias, las pide; si no, arma la planilla."""
    datos = albor.leer_tanda(tanda)
    refs = albor.referencias()
    if not datos or not refs:
        return _pagina_albor(request, usuario, 400, error="La tanda venció o falta la plantilla: volvé a subir el archivo.")
    faltan = albor.pendientes(datos["ingresos"], refs)
    if faltan:
        return plantillas.TemplateResponse(
            request, "albor_equivalencias.html",
            _contexto(request, usuario, seccion="albor", tanda=tanda, origen=datos["origen"],
                      grupos=_grupos_equivalencias({t: faltan[t] for t in faltan}), listas=refs["listas"]),
        )
    resultado = albor.convertir(datos["ingresos"], refs)
    nombre = ""
    if resultado.validas:
        purgar_todo()
        nombre = f"albor_cosecha_{datetime.now():%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:6]}.xlsx"
        albor.escribir(resultado.validas, refs, SALIDA_DIR / nombre)
    log.info("Usuario %s convirtió %s ingresos de SoftCereal a Albor (%s con error)",
             usuario.get("email") or usuario.get("name"), len(resultado.filas), len(resultado.con_error))
    return plantillas.TemplateResponse(
        request, "albor_resultado.html",
        _contexto(request, usuario, seccion="albor", resultado=resultado, archivo=nombre,
                  origen=datos["origen"], columnas=albor.COLUMNAS_USADAS),
    )


def _grupos_equivalencias(items: dict[str, list[dict]]) -> list[dict]:
    """Para el formulario: un grupo por tipo, en el orden de TIPOS_EQUIVALENCIA."""
    return [
        {"tipo": tipo, "titulo": titulo, "lista": lista, "obligatoria": obligatoria, "entradas": items[tipo]}
        for tipo, titulo, lista, obligatoria in albor.TIPOS_EQUIVALENCIA if items.get(tipo)
    ]


@app.get("/albor/equivalencias", response_class=HTMLResponse)
def ver_equivalencias(request: Request):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    refs = albor.referencias() or {"listas": {clave: [] for clave in albor.LISTAS}}
    guardadas = albor.equivalencias()
    items = {
        tipo: [{"clave": k, "sugerencia": v, "filas": None} for k, v in sorted(guardadas[tipo].items())]
        for tipo in guardadas
    }
    return plantillas.TemplateResponse(
        request, "albor_equivalencias.html",
        _contexto(request, usuario, seccion="albor", tanda="", grupos=_grupos_equivalencias(items),
                  listas=refs["listas"]),
    )


@app.post("/albor/equivalencias", response_class=HTMLResponse)
async def guardar_equivalencias(request: Request):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    formulario = await request.form()
    tipos = {t for t, *_ in albor.TIPOS_EQUIVALENCIA}
    nuevas: dict[str, dict[str, str]] = {}
    for nombre, valor in formulario.items():
        m = re.fullmatch(r"k(\d+)", nombre)
        if not m:
            continue
        tipo, _, clave_eq = str(valor).partition("::")
        if tipo in tipos and clave_eq:
            nuevas.setdefault(tipo, {})[clave_eq] = str(formulario.get(f"v{m.group(1)}", ""))
    tanda = str(formulario.get("tanda", ""))
    if tanda:
        # En una tanda, lo que se deja vacío queda pendiente: no se borra nada.
        nuevas = {t: {k: v for k, v in vals.items() if v.strip()} for t, vals in nuevas.items()}
    albor.guardar_equivalencias(nuevas)
    if tanda:
        return _seguir_tanda(request, usuario, tanda)
    return RedirectResponse("/albor/equivalencias?ok=1", status_code=303)


@app.get("/egresos", response_class=HTMLResponse)
def egresos(request: Request):
    """Solapa de egresos: todavia sin construir."""
    usuario = usuario_de(request)
    if not usuario:
        respuesta = _sin_sesion(request)
        if isinstance(respuesta, HTTPException):
            raise respuesta
        return respuesta
    return plantillas.TemplateResponse(request, "egresos.html", _contexto(request, usuario, seccion="egresos"))


@app.get("/descargar/{nombre}")
def descargar(request: Request, nombre: str):
    if not usuario_de(request):
        raise HTTPException(status_code=401, detail="Sesión no válida")
    # El nombre lo genera la app, pero igual se valida: viene por la URL.
    if not re.fullmatch(r"(visec_cpe|albor_cosecha)_[0-9_a-f]+\.xlsx", nombre):
        raise HTTPException(status_code=400, detail="Nombre de archivo inválido")
    ruta = SALIDA_DIR / nombre
    if not ruta.is_file():
        raise HTTPException(status_code=404, detail="El archivo ya no está disponible")
    return FileResponse(
        ruta,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=nombre,
    )


@app.get("/salud")
def salud():
    """Healthcheck del contenedor: 503 si falta configuracion o un certificado.

    Que el puerto responda no alcanza: sin certificado la app no puede consultar
    ARCA y conviene que el contenedor figure unhealthy.
    """
    problemas = config.validar()
    cuerpo = {
        "estado": "ok" if not problemas else "configuracion incompleta",
        "entorno": config.entorno,
        "problemas": problemas,
    }
    return JSONResponse(cuerpo, status_code=200 if not problemas else 503)

