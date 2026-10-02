"""Interfaz para informar CPE a Visec.

Se pegan (o se suben) los números de CTG, la app consulta cada CPE en el WS de
ARCA y arma el XLSX con la plantilla de Visec.
"""

from __future__ import annotations

import csv
import logging
from contextlib import asynccontextmanager
import os
import re
import time
import unicodedata
import uuid
from datetime import datetime
from pathlib import Path

import jwt
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .config import BASE_DIR, config
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
    yield


app = FastAPI(title="Informar CPE a Visec", lifespan=ciclo_de_vida)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
plantillas = Jinja2Templates(directory=str(BASE_DIR / "templates"))


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
}


def _normalizar(nombre: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", sin_tildes.strip().lower()).strip("_")


def parsear_entrada(texto: str) -> tuple[list[tuple[str, dict[str, str]]], list[str]]:
    """Devuelve [(ctg, datos_propios)] y los avisos del parseo.

    Acepta una lista de CTG (uno por línea) o el CSV que genera
    `scripts/ctg_soja.py`, del que además toma la fecha del movimiento y el
    peso que entró a stock.
    """
    lineas = [l for l in texto.splitlines() if l.strip()]
    if not lineas:
        return [], []

    primera = lineas[0]
    delimitador = ";" if ";" in primera else ("\t" if "\t" in primera else ",")
    filas = list(csv.reader(lineas, delimiter=delimitador))

    columnas: dict[str, int] = {}
    if filas and not re.fullmatch(r"\d{6,}", filas[0][0].strip()):
        for indice, nombre in enumerate(filas[0]):
            clave = ALIAS.get(_normalizar(nombre))
            if clave and clave not in columnas:
                columnas[clave] = indice
        filas = filas[1:]
    columnas.setdefault("ctg", 0)

    resultado: list[tuple[str, dict[str, str]]] = []
    vistos: set[str] = set()
    avisos: list[str] = []
    for numero, fila in enumerate(filas, start=1):
        if not fila:
            continue
        indice_ctg = columnas["ctg"]
        ctg = re.sub(r"\D", "", fila[indice_ctg] if indice_ctg < len(fila) else "")
        if not ctg:
            avisos.append(f"Línea {numero}: no se encontró un número de CTG ({fila[:3]})")
            continue
        if ctg in vistos:
            avisos.append(f"CTG {ctg}: repetido, se informa una sola vez")
            continue
        vistos.add(ctg)
        propios = {
            clave: fila[indice].strip()
            for clave, indice in columnas.items()
            if clave != "ctg" and indice < len(fila) and fila[indice].strip()
        }
        resultado.append((ctg, propios))
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


@app.get("/descargar/{nombre}")
def descargar(request: Request, nombre: str):
    if not usuario_de(request):
        raise HTTPException(status_code=401, detail="Sesión no válida")
    # El nombre lo genera la app, pero igual se valida: viene por la URL.
    if not re.fullmatch(r"visec_cpe_[0-9_a-f]+\.xlsx", nombre):
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

