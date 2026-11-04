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
from datetime import datetime, timedelta
from html import escape as html_escape
from pathlib import Path

import jwt
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, config
from . import albor
from . import oracle
from . import bolsatech
from . import visec_api
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
    tareas = [asyncio.create_task(_vigilar_certificados())]
    if config.bolsatech_automatico:
        log.info("BolsaTech automático encendido: cada %s horas", config.bolsatech_cada_horas)
        tareas.append(asyncio.create_task(_informar_bolsatech_solo()))
    if config.visec_api_automatico:
        log.info("Visec por API automático encendido (%s): cada %s minutos",
                 config.visec_api_ambiente, config.visec_api_cada_minutos)
        tareas.append(asyncio.create_task(_informar_visec_solo()))
    yield
    for tarea in tareas:
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


async def _informar_bolsatech_solo() -> None:
    """La tarea programada que antes corría cpe_bolsatech.py.

    Solo arranca con BOLSATECH_AUTOMATICO=true: esto informa a un sistema
    externo sin que nadie mire, así que tiene que ser una decisión explícita.
    """
    # Un rato después de arrancar, para no pegarle a Oracle mientras levanta.
    await asyncio.sleep(60)
    while True:
        try:
            resumen = await asyncio.to_thread(bolsatech.corrida)
            informadas = sum(1 for r in resumen["resultados"]
                             if r["estado"] == bolsatech.INFORMADA)
            log.info("Corrida de BolsaTech: %s encontradas, %s informadas, corte %s",
                     resumen["encontradas"], informadas,
                     "avanzó" if resumen["avanzo"] else "sin cambios")
            bolsatech.anotar_automatica(resumen)
            if resumen["resultados"] or resumen["errores_datos"]:
                await asyncio.to_thread(_avisar_corrida, resumen)
        except Exception as error:  # un error acá no puede tirar abajo la app
            log.exception("Falló la corrida automática de BolsaTech")
            bolsatech.anotar_automatica(error=str(error))
        await asyncio.sleep(max(config.bolsatech_cada_horas, 1) * 3600)


async def _informar_visec_solo() -> None:
    """Informa a Visec por API cada VISEC_API_CADA_MINUTOS.

    Primero revisa los procesos que quedaron esperando y después manda lo
    nuevo. Si una corrida todavía no terminó cuando toca la siguiente, esa se
    saltea: corrida() no deja que dos se pisen.
    """
    await asyncio.sleep(90)  # después de BolsaTech, para no arrancar las dos juntas
    while True:
        try:
            resumen = await asyncio.to_thread(visec_api.corrida, True)
            visec_api.anotar_automatica(resumen)
            log.info("Visec API: %s encontradas, %s, procesos %s", resumen["encontradas"],
                     resumen["por_estado"], resumen["procesos"])
            rechazadas = (resumen["procesos"].get(visec_api.RECHAZADA, 0)
                          + resumen["por_estado"].get(visec_api.RECHAZADA, 0))
            if rechazadas:
                await asyncio.to_thread(_avisar_rechazos_visec, resumen, rechazadas)
        except Exception as error:  # un error acá no puede tirar abajo la app
            log.exception("Falló la corrida automática de Visec por API")
            visec_api.anotar_automatica(error=str(error))
        await asyncio.sleep(max(config.visec_api_cada_minutos, 1) * 60)


def _avisar_rechazos_visec(resumen: dict, cuantas: int) -> None:
    """Solo avisa cuando Visec rechaza algo: un mail cada 15 minutos con "todo
    bien" sería ruido y dejaría de leerse."""
    if problemas_correo():
        return
    rechazadas = [(ctg, d) for ctg, d in visec_api.estado().items()
                  if d.get("estado") == visec_api.RECHAZADA]
    filas = "".join(
        f"<tr><td>{ctg}</td><td>{html_escape(d.get('detalle', ''))}</td></tr>"
        for ctg, d in rechazadas[-50:])
    url = f"{config.app_url}/visec-api" if config.app_url else "/visec-api"
    cuerpo = (f"<p>Visec ({config.visec_api_ambiente}) rechazó {cuantas} CPE en la última corrida. "
              f"No se reintentan solas: revisalas y reintentalas desde <a href=\"{url}\">{url}</a>.</p>"
              f"<table border=\"1\" cellpadding=\"6\" style=\"border-collapse: collapse\">"
              f"<tr><th>CTG</th><th>Motivo</th></tr>{filas}</table>")
    try:
        enviar_mail(f"Visec rechazó {cuantas} CPE", re.sub(r"<[^>]+>", " ", cuerpo), cuerpo)
    except ErrorCorreo as error:
        log.warning("No se pudo avisar los rechazos de Visec: %s", error)


def _avisar_corrida(resumen: dict) -> None:
    if problemas_correo():
        return
    asunto, html = bolsatech.resumen_a_texto(resumen)
    try:
        enviar_mail(asunto, re.sub(r"<[^>]+>", " ", html), html)
    except ErrorCorreo as error:
        log.warning("No se pudo enviar el resumen de BolsaTech: %s", error)


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
                  equivalencias=albor.equivalencias(),
                  oracle_listo=config.oracle_listo, problemas_oracle=config.problemas_oracle(),
                  esquemas=config.oracle_esquemas, ticket_desde=albor.TICKET_DESDE, **extra),
        status_code=status_code,
    )


@app.get("/albor", response_class=HTMLResponse)
def ver_albor(request: Request):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    return _pagina_albor(request, usuario)


@app.post("/albor", response_class=HTMLResponse)
async def generar_albor(
    request: Request,
    ctgs: str = Form(""),
    archivo: UploadFile | None = File(None),
    plantilla: UploadFile | None = File(None),
    listado: UploadFile | None = File(None),
):
    """Los CTG salen del ERP (Oracle) o, si no hay base configurada, de la
    exportación de SoftCereal. La plantilla de Albor y el listado de
    comprobantes son archivos, y se reconocen por su contenido: da igual en qué
    recuadro se hayan soltado."""
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario

    def error(mensaje: str):
        return _pagina_albor(request, usuario, 400, error=mensaje)

    softcereal, nombre = None, ""
    cargados: set[str] | None = None  # CTG que ya están en Albor, si subieron el listado
    for subido in (archivo, plantilla, listado):
        if not subido or not subido.filename:
            continue
        contenido = await subido.read()
        if not contenido:
            continue
        if albor.es_plantilla(contenido):
            try:
                await asyncio.to_thread(albor.guardar_plantilla, contenido)
            except albor.ErrorArchivo as e:
                return error(str(e))
            except Exception as e:
                log.warning("No se pudo leer la plantilla de Albor %s: %s", subido.filename, e)
                return error(f"No se pudo leer la plantilla de Albor: {e}")
            log.info("Usuario %s cargó la plantilla de Albor %s", usuario.get("email") or usuario.get("name"), subido.filename)
        elif albor.es_listado_albor(contenido):
            cargados = (cargados or set()) | albor.ctg_en_listado(contenido)
        elif es_planilla(contenido):
            softcereal, nombre = contenido, subido.filename
        else:
            return error(
                f"«{subido.filename}» no es ninguno de los archivos que se esperan: la exportación "
                "de SoftCereal, la plantilla ImportacionCosecha.xlsx o el listado de comprobantes de Albor."
            )

    if not albor.referencias():
        return error("Falta la plantilla de importación de Albor: soltá el ImportacionCosecha.xlsx que bajás de Albor.")
    pares, sueltas = albor.parsear_ctgs(ctgs)
    if (ctgs or "").strip() and not pares:
        return error("No encontré ningún CTG. Son 11 dígitos, uno por línea, con el número de ticket al lado.")
    pedidos = [c for c, _ in pares]
    if not pedidos and softcereal is None:
        if cargados is not None:
            return error("Con el listado de Albor hacen falta también los CTG.")
        return _pagina_albor(request, usuario, ok_plantilla="Plantilla de Albor cargada. Ahora pegá los CTG.")

    faltantes: list[str] = []
    if pedidos:
        if len(pedidos) > MAX_CTG:
            return error(f"Son {len(pedidos)} CTG y el máximo por tanda es {MAX_CTG}.")
        try:
            filas = await asyncio.to_thread(oracle.buscar, pedidos)
        except oracle.ErrorOracle as e:
            return error(str(e))
        except Exception as e:
            log.exception("Error consultando Oracle")
            return error(f"Error consultando el ERP: {e}")
        ingresos = albor.leer_oracle(filas)
        # El número de ticket lo escribe el usuario junto al CTG.
        tickets = dict(pares)
        for ingreso in ingresos:
            ingreso["ticket"] = tickets.get(ingreso["ctg"], "")
        encontrados = {i["ctg"] for i in ingresos}
        faltantes = [c for c in pedidos if c not in encontrados]
        nombre = f"{len(ingresos)} CTG del ERP"
        if not ingresos:
            return error(
                "Ninguno de esos CTG está en el ERP. Revisá los números, o que el "
                "ingreso ya esté cargado en SoftCereal."
            )
    else:
        try:
            ingresos = albor.leer_softcereal(filas_planilla(softcereal))
        except albor.ErrorArchivo as e:
            return error(str(e))
        except Exception as e:  # planilla dañada o de un formato raro
            log.warning("No se pudo leer %s: %s", nombre, e)
            return error(f"No se pudo leer la planilla: {e}")
    ya_en_albor: list[dict] = []
    if cargados:
        ingresos, ya_en_albor = albor.separar_cargados(ingresos, cargados)
    if sueltas:
        log.info("Líneas sin CTG en la entrada: %s", len(sueltas))
    tanda = albor.guardar_tanda(ingresos, nombre or "", ya_en_albor, faltantes)
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
                      grupos=_grupos_equivalencias({t: faltan[t] for t in faltan}),
                      listas=dict(refs["listas"], cultivo=albor.lista_cultivos(refs), campo=albor.campos_albor())),
        )
    resultado = albor.convertir(datos["ingresos"], refs, datos.get("ya_en_albor"))
    faltantes = datos.get("faltantes") or []
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
                  origen=datos["origen"], columnas=albor.COLUMNAS_USADAS, faltantes=faltantes),
    )


def _grupos_equivalencias(items: dict[str, list[dict]]) -> list[dict]:
    """Para el formulario: un grupo por tipo, en el orden de TIPOS_EQUIVALENCIA."""
    return [
        {"tipo": tipo, "titulo": titulo, "lista": lista, "obligatoria": obligatoria,
         "ayuda": ayuda, "entradas": items[tipo]}
        for tipo, titulo, lista, obligatoria, ayuda in albor.TIPOS_EQUIVALENCIA if items.get(tipo)
    ]


def formulario_filas(formulario) -> list[dict]:
    """Las filas de la tabla de cultivos: campos con nombre «c3_codigo», etc."""
    filas: dict[str, dict] = {}
    for nombre, valor in formulario.items():
        m = re.fullmatch(r"c(\d+)_([a-z_]+)", nombre)
        if m:
            filas.setdefault(m.group(1), {})[m.group(2)] = str(valor)
    return [filas[k] for k in sorted(filas, key=int)]


@app.get("/albor/cultivos", response_class=HTMLResponse)
def ver_cultivos(request: Request):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    return _pagina_cultivos(request, usuario)


def _pagina_cultivos(request: Request, usuario: dict, **extra):
    refs = albor.referencias()
    return plantillas.TemplateResponse(
        request, "albor_cultivos.html",
        _contexto(request, usuario, seccion="albor",
                  cargados=albor.cultivos_cargados(),
                  campos=albor.campos_albor(),
                  actividades=albor.actividades_albor(),
                  de_plantilla=(refs or {}).get("listas", {}).get("cultivo", []),
                  **extra),
    )


@app.post("/albor/cultivos", response_class=HTMLResponse)
async def guardar_cultivos(request: Request, archivo: UploadFile | None = File(None),
                           cultivos: str = Form("")):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    formulario = await request.form()
    if archivo is not None and archivo.filename:
        contenido = await archivo.read()
        if not albor.es_export_cultivos(contenido):
            return _pagina_cultivos(
                request, usuario,
                error=f"«{archivo.filename}» no es la exportación de cultivos de Albor "
                      "(tiene que tener las columnas ID_Cultivo, Nombre, Campo y Lote).")
        try:
            lista = await asyncio.to_thread(albor.importar_cultivos, contenido)
        except albor.ErrorArchivo as e:
            return _pagina_cultivos(request, usuario, error=str(e))
        except Exception as e:
            log.warning("No se pudo leer %s: %s", archivo.filename, e)
            return _pagina_cultivos(request, usuario, error=f"No se pudo leer el archivo: {e}")
        log.info("Usuario %s importó %s cultivos de Albor",
                 usuario.get("email") or usuario.get("name"), len(lista))
        campanas = ", ".join(sorted({c.get("campana") for c in lista if c.get("campana")})) or "sin campaña"
        return _pagina_cultivos(request, usuario,
                                ok=f"{len(lista)} cultivos importados. Campañas: {campanas}.")
    filas = formulario_filas(formulario)
    if filas:
        lista = albor.guardar_filas_cultivos(filas)
        log.info("Usuario %s guardó %s cultivos a mano",
                 usuario.get("email") or usuario.get("name"), len(lista))
        return _pagina_cultivos(request, usuario, ok=f"{len(lista)} cultivos guardados.")
    lista = albor.guardar_cultivos(cultivos)
    log.info("Usuario %s guardó %s cultivos de Albor",
             usuario.get("email") or usuario.get("name"), len(lista))
    return _pagina_cultivos(request, usuario, ok=f"{len(lista)} cultivos guardados.")


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
                  listas=dict(refs["listas"], cultivo=albor.lista_cultivos(refs), campo=albor.campos_albor())),
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
        # En una tanda no se borra nada. Lo que se deja vacío queda pendiente,
        # salvo en las equivalencias opcionales: ahí vacío significa "acá no va
        # nada" y se guarda así, o se preguntaría en cada tanda.
        nuevas = {
            t: {k: (v.strip() or albor.SIN_EQUIVALENCIA)
                for k, v in vals.items() if v.strip() or t in albor.OPCIONALES}
            for t, vals in nuevas.items()
        }
    albor.guardar_equivalencias(nuevas)
    if tanda:
        return _seguir_tanda(request, usuario, tanda)
    return RedirectResponse("/albor/equivalencias?ok=1", status_code=303)


# ---------------------------------------------------------------------------
# BolsaTech
# ---------------------------------------------------------------------------
# Esta sección escribe en un sistema externo: consultar es inofensivo, informar
# no se deshace. Por eso son dos pasos y dos botones distintos.

PREFIJOS_MUESTRA = [
    ("", "Según la planta de cada ingreso"),
    ("7790", "Baradero (7790)"),
    ("5140", "Alberti (5140)"),
]


def _proxima_automatica(ultima: dict | None):
    """Cuándo volvería a correr sola. Aproximado: el ciclo duerme desde que
    terminó la anterior, así que una corrida larga la corre un poco."""
    if not ultima:
        return None
    return ultima["momento"] + timedelta(hours=max(config.bolsatech_cada_horas, 1))


def _pagina_bolsatech(request: Request, usuario: dict, status_code: int = 200, **extra):
    ultima_auto = bolsatech.ultima_automatica()
    return plantillas.TemplateResponse(
        request, "bolsatech.html",
        _contexto(request, usuario, seccion="bolsatech",
                  listo=config.bolsatech_listo,
                  problemas_bolsatech=config.problemas_bolsatech(),
                  prefijos=PREFIJOS_MUESTRA,
                  informadas=len(bolsatech.informadas()),
                  automatico=config.bolsatech_automatico,
                  cada_horas=config.bolsatech_cada_horas,
                  desde_cuando=_desde_cuando_seguro(),
                  ultima_automatica=ultima_auto,
                  proxima_automatica=_proxima_automatica(ultima_auto),
                  **extra),
        status_code=status_code,
    )


def _desde_cuando_seguro():
    try:
        return bolsatech.desde_cuando()
    except bolsatech.ErrorBolsaTech:
        return None


@app.get("/bolsatech", response_class=HTMLResponse)
def ver_bolsatech(request: Request):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    return _pagina_bolsatech(request, usuario)


@app.post("/bolsatech", response_class=HTMLResponse)
async def consultar_bolsatech(request: Request, ctgs: str = Form(""),
                              prefijo: str = Form("")):
    """Paso 1: busca los ingresos y muestra qué se informaría. No envía nada."""
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    if not config.oracle_listo:
        return _pagina_bolsatech(request, usuario, 400,
                                 error="Falta configurar el ERP: " +
                                       "; ".join(config.problemas_oracle()))
    pedidos = list(dict.fromkeys(CTG.findall(ctgs or "")))
    if not pedidos:
        return _pagina_bolsatech(request, usuario, 400,
                                 error="No encontré ningún CTG. Son 11 dígitos, uno por línea.")
    if len(pedidos) > MAX_CTG:
        return _pagina_bolsatech(request, usuario, 400,
                                 error=f"Son {len(pedidos)} CTG y el máximo por tanda es {MAX_CTG}.")
    if prefijo not in {p for p, _ in PREFIJOS_MUESTRA}:
        prefijo = ""
    try:
        filas = await asyncio.to_thread(oracle.buscar_bolsatech, pedidos)
    except oracle.ErrorOracle as e:
        return _pagina_bolsatech(request, usuario, 400, error=str(e))
    except Exception as e:
        log.exception("Error consultando Oracle para BolsaTech")
        return _pagina_bolsatech(request, usuario, 400, error=f"Error consultando el ERP: {e}")

    encontrados = {re.sub(r"\D", "", str(f.get("ctg") or "")) for f in filas}
    faltantes = [c for c in pedidos if c not in encontrados]
    if not filas:
        return _pagina_bolsatech(request, usuario, 400,
                                 error="Ninguno de esos CTG está en el ERP.")
    tanda = bolsatech.guardar_tanda(filas, prefijo, faltantes)
    return _resumen_bolsatech(request, usuario, tanda)


def _resumen_bolsatech(request: Request, usuario: dict, tanda: str, **extra):
    datos = bolsatech.leer_tanda(tanda)
    if not datos:
        return _pagina_bolsatech(request, usuario, 400,
                                 error="La tanda venció: volvé a pegar los CTG.")
    ya = bolsatech.informadas()
    cpes, repetidas = [], []
    for fila in datos["filas"]:
        cpe = bolsatech.armar(fila, datos.get("prefijo", ""))
        (repetidas if cpe.ctg in ya else cpes).append(cpe)
    return plantillas.TemplateResponse(
        request, "bolsatech_resumen.html",
        _contexto(request, usuario, seccion="bolsatech", tanda=tanda,
                  cpes=cpes, repetidas=repetidas,
                  faltantes=datos.get("faltantes") or [],
                  listas=[c for c in cpes if c.ok],
                  con_error=[c for c in cpes if not c.ok],
                  estado=bolsatech.estado(), **extra),
    )


@app.post("/bolsatech/informar", response_class=HTMLResponse)
async def informar_bolsatech(request: Request, tanda: str = Form(""),
                             confirmo: str = Form("")):
    """Paso 2: envía. Es lo único de la app que escribe afuera."""
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    datos = bolsatech.leer_tanda(tanda)
    if not datos:
        return _pagina_bolsatech(request, usuario, 400,
                                 error="La tanda venció: volvé a pegar los CTG.")
    if confirmo != "si":
        return _resumen_bolsatech(request, usuario, tanda,
                                  error="Marcá la casilla para confirmar el envío.")
    if not config.bolsatech_listo:
        return _resumen_bolsatech(request, usuario, tanda,
                                  error="Falta configurar BolsaTech: " +
                                        "; ".join(config.problemas_bolsatech()))

    ya = bolsatech.informadas()
    pendientes = [c for c in (bolsatech.armar(f, datos.get("prefijo", ""))
                              for f in datos["filas"])
                  if c.ok and c.ctg not in ya]
    resultados = []
    for cpe in pendientes:
        try:
            situacion, track_id, detalle = await asyncio.to_thread(bolsatech.informar, cpe)
        except bolsatech.ErrorBolsaTech as e:
            bolsatech.anotar(cpe, bolsatech.ERROR, detalle=str(e))
            situacion, track_id, detalle = bolsatech.ERROR, "", str(e)
        except Exception as e:
            log.exception("Error informando el CTG %s", cpe.ctg)
            bolsatech.anotar(cpe, bolsatech.ERROR, detalle=str(e))
            situacion, track_id, detalle = bolsatech.ERROR, "", str(e)
        resultados.append({"cpe": cpe, "estado": situacion,
                           "track_id": track_id, "detalle": detalle})
    log.info("Usuario %s informó %s CPE a BolsaTech",
             usuario.get("email") or usuario.get("name"), len(resultados))
    return plantillas.TemplateResponse(
        request, "bolsatech_resultado.html",
        _contexto(request, usuario, seccion="bolsatech", resultados=resultados,
                  INFORMADA=bolsatech.INFORMADA, YA_ESTABA=bolsatech.YA_ESTABA,
                  SIN_CONFIRMAR=bolsatech.SIN_CONFIRMAR, ERROR=bolsatech.ERROR),
    )


def _fecha_form(valor: str, campo: str, fin_del_dia: bool = False):
    """Lo que manda un <input type=date>, o None si vino vacío."""
    valor = (valor or "").strip()
    if not valor:
        return None
    try:
        fecha = datetime.strptime(valor, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"La fecha «{campo}» no es una fecha válida.")
    return fecha.replace(hour=23, minute=59, second=59) if fin_del_dia else fecha


@app.post("/bolsatech/corrida", response_class=HTMLResponse)
async def correr_bolsatech(request: Request, confirmo: str = Form(""),
                           desde: str = Form(""), hasta: str = Form("")):
    """Corre ahora la búsqueda por fechas, sin esperar al automático."""
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    solo_ver = confirmo != "si"
    if not solo_ver and not config.bolsatech_listo:
        return _pagina_bolsatech(request, usuario, 400,
                                 error="Falta configurar BolsaTech: " +
                                       "; ".join(config.problemas_bolsatech()))
    try:
        desde_fecha = _fecha_form(desde, "desde")
        hasta_fecha = _fecha_form(hasta, "hasta", fin_del_dia=True)
    except ValueError as e:
        return _pagina_bolsatech(request, usuario, 400, error=str(e))
    try:
        resumen = await asyncio.to_thread(bolsatech.corrida, not solo_ver,
                                          desde_fecha, hasta_fecha)
    except bolsatech.ErrorBolsaTech as e:
        return _pagina_bolsatech(request, usuario, 400, error=str(e))
    except oracle.ErrorOracle as e:
        return _pagina_bolsatech(request, usuario, 400, error=str(e))
    except Exception as e:
        log.exception("Falló la corrida de BolsaTech")
        return _pagina_bolsatech(request, usuario, 400, error=f"Falló la corrida: {e}")
    if not solo_ver:
        log.info("Usuario %s corrió la búsqueda por fechas de BolsaTech",
                 usuario.get("email") or usuario.get("name"))
    return plantillas.TemplateResponse(
        request, "bolsatech_corrida.html",
        _contexto(request, usuario, seccion="bolsatech", r=resumen, solo_ver=solo_ver,
                  prefijo_mliaudat=config.bolsatech_prefijo_mliaudat,
                  INFORMADA=bolsatech.INFORMADA, YA_ESTABA=bolsatech.YA_ESTABA,
                  SIN_CONFIRMAR=bolsatech.SIN_CONFIRMAR, ERROR=bolsatech.ERROR),
    )


# ---------------------------------------------------------------------------
# Visec por API
# ---------------------------------------------------------------------------

def _pagina_visec_api(request: Request, usuario: dict, status_code: int = 200, **extra):
    datos = visec_api.estado()
    pendientes = {e: [] for e in (visec_api.RECHAZADA, visec_api.ENVIADA, visec_api.FALTAN_DATOS,
                                  visec_api.ESPERANDO_ARCA, visec_api.ERROR)}
    for ctg, d in sorted(datos.items(), key=lambda x: x[1].get("cuando", ""), reverse=True):
        if d.get("estado") in pendientes:
            pendientes[d["estado"]].append(dict(d, ctg=ctg))
    ultima = visec_api.ultima_automatica()
    try:
        periodo = visec_api.periodo()
    except visec_api.ErrorVisecApi:
        periodo = None
    return plantillas.TemplateResponse(
        request, "visec_api.html",
        _contexto(request, usuario, seccion="visec_api",
                  visec_ambiente=config.visec_api_ambiente,
                  problemas=config.problemas_visec_api(),
                  automatico=config.visec_api_automatico,
                  cada_minutos=config.visec_api_cada_minutos,
                  ventana_dias=config.visec_api_ventana_dias,
                  especies=config.visec_api_especies,
                  periodo=periodo, ultima=ultima,
                  proxima=(ultima["momento"] + timedelta(minutes=max(config.visec_api_cada_minutos, 1)))
                          if ultima else None,
                  conteo=visec_api.resumen_estado(), pendientes=pendientes,
                  E=visec_api, **extra),
        status_code=status_code,
    )


@app.get("/visec-api", response_class=HTMLResponse)
def ver_visec_api(request: Request):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    return _pagina_visec_api(request, usuario)


@app.post("/visec-api/probar", response_class=HTMLResponse)
async def probar_visec_api(request: Request):
    """Pide un token: prueba credenciales y red sin mandar ninguna CPE."""
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    try:
        await asyncio.to_thread(visec_api.token, True)
    except visec_api.ErrorVisecApi as e:
        return _pagina_visec_api(request, usuario, 400, error=f"No conectó: {e}")
    return _pagina_visec_api(request, usuario, ok=f"Conectó con Visec ({config.visec_api_ambiente}): "
                                                  "las credenciales andan y dio un token.")


@app.post("/visec-api/corrida", response_class=HTMLResponse)
async def correr_visec_api(request: Request, confirmo: str = Form(""), ctgs: str = Form(""),
                           desde: str = Form(""), hasta: str = Form("")):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    solo_ver = confirmo != "si"
    if not config.oracle_listo:
        return _pagina_visec_api(request, usuario, 400,
                                 error="Falta configurar el ERP: " + "; ".join(config.problemas_oracle()))
    if not solo_ver and not config.visec_api_listo:
        return _pagina_visec_api(request, usuario, 400,
                                 error="Falta configurar Visec: " + "; ".join(config.problemas_visec_api()))
    pedidos = list(dict.fromkeys(re.findall(r"\b\d{11}\b", ctgs or "")))
    try:
        desde_fecha = _fecha_form(desde, "desde")
        hasta_fecha = _fecha_form(hasta, "hasta", fin_del_dia=True)
    except ValueError as e:
        return _pagina_visec_api(request, usuario, 400, error=str(e))
    try:
        resumen = await asyncio.to_thread(visec_api.corrida, not solo_ver,
                                          desde_fecha, hasta_fecha, pedidos or None)
    except (visec_api.ErrorVisecApi, oracle.ErrorOracle) as e:
        return _pagina_visec_api(request, usuario, 400, error=str(e))
    except Exception as e:
        log.exception("Falló la corrida de Visec por API")
        return _pagina_visec_api(request, usuario, 400, error=f"Falló la corrida: {e}")
    if not solo_ver:
        log.info("Usuario %s corrió Visec por API (%s)",
                 usuario.get("email") or usuario.get("name"), config.visec_api_ambiente)
    return plantillas.TemplateResponse(
        request, "visec_api_corrida.html",
        _contexto(request, usuario, seccion="visec_api", r=resumen, solo_ver=solo_ver,
                  visec_ambiente=config.visec_api_ambiente, E=visec_api),
    )


@app.post("/visec-api/reintentar")
async def reintentar_visec_api(request: Request):
    """Vuelve a la cola las rechazadas que se marquen, una vez corregidas."""
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    formulario = await request.form()
    ctgs = [c for c in formulario.getlist("ctg") if re.fullmatch(r"\d{11}", c)]
    cuantos = visec_api.olvidar(ctgs)
    return RedirectResponse(f"/visec-api?reintentar={cuantos}", status_code=303)


@app.get("/bolsatech/pendientes", response_class=HTMLResponse)
def ver_pendientes(request: Request):
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    return _pagina_pendientes(request, usuario)


def _pagina_pendientes(request: Request, usuario: dict, status_code: int = 200, **extra):
    return plantillas.TemplateResponse(
        request, "bolsatech_pendientes.html",
        _contexto(request, usuario, seccion="bolsatech",
                  pendientes=bolsatech.pendientes_de_confirmar(),
                  hay_track_url=bool(config.bolsatech_url_track), **extra),
        status_code=status_code,
    )


@app.post("/bolsatech/importar-control", response_class=HTMLResponse)
async def importar_control(request: Request, archivo: UploadFile = File(...)):
    """Carga el cpe_control.csv del script viejo, para recuperar los trackId."""
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    contenido = await archivo.read()
    try:
        conteo = bolsatech.importar_control(contenido)
    except bolsatech.ErrorBolsaTech as e:
        return _pagina_pendientes(request, usuario, 400, error=str(e))
    except Exception as e:
        log.warning("No se pudo leer %s: %s", archivo.filename, e)
        return _pagina_pendientes(request, usuario, 400, error=f"No se pudo leer el archivo: {e}")
    log.info("Usuario %s importó el control de BolsaTech: %s",
             usuario.get("email") or usuario.get("name"), conteo)
    detalle = ", ".join(f"{n} {k.replace('_', ' ')}" for k, n in sorted(conteo.items()))
    return _pagina_pendientes(request, usuario, ok=f"Control importado: {detalle or 'sin filas'}.")


@app.post("/bolsatech/confirmar", response_class=HTMLResponse)
async def confirmar_pendientes(request: Request, confirmo: str = Form("")):
    """Confirma los trackId que quedaron pendientes. Vuelve a hablar con
    BolsaTech, pero solo confirma recepciones: no da de alta nada nuevo."""
    usuario = _con_sesion(request)
    if isinstance(usuario, RedirectResponse):
        return usuario
    if confirmo != "si":
        return _pagina_pendientes(request, usuario, 400,
                                  error="Marcá la casilla para confirmar.")
    if not config.bolsatech_url_track:
        return _pagina_pendientes(request, usuario, 400,
                                  error="Falta BOLSATECH_TRACK_URL en el .env.")
    resultados = []
    for item in bolsatech.pendientes_de_confirmar():
        situacion, detalle = await asyncio.to_thread(
            bolsatech.confirmar_pendiente, item["ctg"])
        resultados.append({"ctg": item["ctg"], "track_id": item.get("track_id", ""),
                           "estado": situacion, "detalle": detalle})
    confirmadas = sum(1 for r in resultados if r["estado"] == bolsatech.INFORMADA)
    log.info("Usuario %s confirmó %s de %s trackId pendientes",
             usuario.get("email") or usuario.get("name"), confirmadas, len(resultados))
    return _pagina_pendientes(
        request, usuario, resultados=resultados,
        ok=f"{confirmadas} de {len(resultados)} confirmadas." if resultados else None)


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

