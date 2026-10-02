"""Cliente del WS de Carta de Porte Electrónica (wscpe) de ARCA.

Solo se usa la consulta: `consultarCPEAutomotor` por número de CTG. La app no
autoriza, anula ni modifica nada, así que no hay forma de alterar una CPE.

El SOAP se arma a mano con la estructura del manual (v2.0.5) en lugar de leer
el WSDL: en producción, `cpea-ws.arca.gob.ar/wscpe/services/soap?wsdl`
responde 200 con el cuerpo vacío, y un cliente que depende del WSDL no puede
ni arrancar. Es lo mismo que hace cpe_bolsatech.py con BolsaTech.

Diagnóstico desde el servidor:
    docker exec informarcpe python -m app.wscpe dummy
    docker exec informarcpe python -m app.wscpe consultar 10135055909
"""

from __future__ import annotations

import json
import logging
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from html import escape

import requests

from .config import config
from .wsaa import ErrorWSAA, autenticar

log = logging.getLogger(__name__)

SOAP_ENV = "http://schemas.xmlsoap.org/soap/envelope/"

# Errores de ARCA que significan "este CUIT no intervino en esta CPE": con
# varios certificados configurados vale la pena reintentar con otro.
TEXTOS_NO_INTERVINIENTE = ("intervin", "no participa", "no se encuentra autorizado", "no autorizado")


class ErrorWSCPE(RuntimeError):
    pass


@dataclass
class Consulta:
    """Resultado de consultar un CTG."""

    ctg: str
    cpe: dict | None = None
    cuit_consultante: str = ""
    errores: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.cpe is not None

    @property
    def estado(self) -> str:
        return str((self.cpe or {}).get("cabecera", {}).get("estado") or "")


# ---------------------------------------------------------------------------
# XML
# ---------------------------------------------------------------------------

def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def a_dict(elemento: ET.Element) -> dict | str:
    """XML -> dict, sin namespaces. Las etiquetas repetidas quedan en lista.

    `<dominio>` y `<error>` pueden venir varias veces; el resto, una.
    """
    hijos = list(elemento)
    if not hijos:
        return (elemento.text or "").strip()
    salida: dict = {}
    for hijo in hijos:
        clave, valor = _local(hijo.tag), a_dict(hijo)
        if clave in salida:
            if not isinstance(salida[clave], list):
                salida[clave] = [salida[clave]]
            salida[clave].append(valor)
        else:
            salida[clave] = valor
    return salida


def _buscar(raiz: ET.Element, nombre: str) -> ET.Element | None:
    return next((e for e in raiz.iter() if _local(e.tag) == nombre), None)


def _sobre(operacion: str, cuerpo: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="{SOAP_ENV}" xmlns:wsc="{config.ns_wscpe}">
  <soapenv:Header/>
  <soapenv:Body>
    <wsc:{operacion}>{cuerpo}</wsc:{operacion}>
  </soapenv:Body>
</soapenv:Envelope>""".encode("utf-8")


def _enviar(sobre: bytes) -> ET.Element:
    """POST del sobre SOAP. Devuelve el Body de la respuesta."""
    try:
        respuesta = requests.post(
            config.url_wscpe,
            data=sobre,
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": '""'},
            timeout=config.timeout,
        )
    except requests.RequestException as error:
        raise ErrorWSCPE(f"No se pudo conectar con wscpe: {error}") from error

    texto = respuesta.text.strip()
    if not texto:
        # Es lo que contesta el balanceador de ARCA cuando no le gusta algo del
        # pedido: 200 y cuerpo vacío, sin ninguna pista.
        raise ErrorWSCPE(f"wscpe respondió vacío (HTTP {respuesta.status_code}) en {config.url_wscpe}")
    try:
        raiz = ET.fromstring(texto)
    except ET.ParseError as error:
        raise ErrorWSCPE(f"wscpe no devolvió XML (HTTP {respuesta.status_code}): {texto[:300]}") from error

    falla = _buscar(raiz, "Fault")
    if falla is not None:
        detalle = a_dict(falla)
        mensaje = detalle.get("faultstring") if isinstance(detalle, dict) else detalle
        raise ErrorWSCPE(f"SOAP Fault: {mensaje or detalle}")

    cuerpo = _buscar(raiz, "Body")
    if cuerpo is None or not list(cuerpo):
        raise ErrorWSCPE(f"Respuesta SOAP sin Body (HTTP {respuesta.status_code}): {texto[:300]}")
    return cuerpo


# ---------------------------------------------------------------------------
# Operaciones
# ---------------------------------------------------------------------------

def dummy() -> dict:
    """Estado del servicio. No necesita ticket: sirve para probar la conexión."""
    cuerpo = _enviar(_sobre("DummyReq", ""))
    respuesta = _buscar(cuerpo, "respuesta")
    return a_dict(respuesta) if respuesta is not None else a_dict(cuerpo)


def _errores_de(respuesta: dict) -> list[str]:
    errores = respuesta.get("errores") or {}
    lista = errores.get("error", []) if isinstance(errores, dict) else errores
    if isinstance(lista, dict):
        lista = [lista]
    mensajes = []
    for item in lista or []:
        if isinstance(item, dict):
            mensajes.append(f"{item.get('codigo', '')}: {item.get('descripcion', '')}".strip(": "))
    return mensajes


def _guardar_respuesta(ctg: str, datos: dict) -> None:
    """Guarda la respuesta. Sirve para auditar y para completar el mapeo."""
    if not config.guardar_respuestas:
        return
    carpeta = config.cache_dir / "respuestas"
    carpeta.mkdir(parents=True, exist_ok=True)
    archivo = carpeta / f"cpe_{ctg}_{datetime.now():%Y%m%d_%H%M%S}.json"
    archivo.write_text(json.dumps(datos, indent=2, ensure_ascii=False), encoding="utf-8")


def _consultar_con(ctg: str, cuit: str) -> tuple[dict | None, list[str]]:
    ticket = autenticar(cuit)
    cuerpo = (
        f"<auth><token>{escape(ticket.token)}</token><sign>{escape(ticket.sign)}</sign>"
        f"<cuitRepresentada>{cuit}</cuitRepresentada></auth>"
        f"<solicitud><cuitSolicitante>{cuit}</cuitSolicitante><nroCTG>{int(ctg)}</nroCTG></solicitud>"
    )
    body = _enviar(_sobre("ConsultarCPEAutomotorReq", cuerpo))
    nodo = _buscar(body, "respuesta")
    respuesta = a_dict(nodo) if nodo is not None else {}
    if not isinstance(respuesta, dict):
        respuesta = {}
    respuesta.pop("pdf", None)  # el PDF en base64 no hace falta y pesa
    errores = _errores_de(respuesta)
    if respuesta.get("cabecera"):
        _guardar_respuesta(ctg, respuesta)
        return respuesta, errores
    return None, errores or ["ARCA no devolvió datos de la CPE"]


def _es_no_interviniente(errores: list[str]) -> bool:
    return any(texto in mensaje.lower() for mensaje in errores for texto in TEXTOS_NO_INTERVINIENTE)


def consultar(ctg: str) -> Consulta:
    """Consulta un CTG probando los certificados configurados."""
    cuits = config.cuits
    if not cuits:
        return Consulta(ctg, errores=["No hay certificados configurados (ARCA_CERTIFICADOS)"])

    ultimos_errores: list[str] = []
    for indice, cuit in enumerate(cuits):
        try:
            cpe, errores = _consultar_con(ctg, cuit)
        except (ErrorWSAA, ErrorWSCPE, OSError, ValueError) as error:
            ultimos_errores = [f"{type(error).__name__}: {error}"]
            log.warning("CTG %s con CUIT %s: %s", ctg, cuit, error)
            continue

        if cpe:
            return Consulta(ctg, cpe=cpe, cuit_consultante=cuit, errores=errores)

        ultimos_errores = errores
        quedan_mas = indice + 1 < len(cuits)
        if not (quedan_mas and _es_no_interviniente(errores)):
            break
        log.info("CTG %s: %s no intervino, se prueba con otro certificado", ctg, cuit)

    return Consulta(ctg, errores=ultimos_errores)


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    orden = sys.argv[1] if len(sys.argv) > 1 else "dummy"
    print(f"wscpe: {config.url_wscpe}  (namespace {config.ns_wscpe})")
    if orden == "dummy":
        print(json.dumps(dummy(), indent=2, ensure_ascii=False))
    elif orden == "consultar" and len(sys.argv) > 2:
        resultado = consultar(sys.argv[2])
        print(json.dumps({"errores": resultado.errores, "cuit": resultado.cuit_consultante,
                          "cpe": resultado.cpe}, indent=2, ensure_ascii=False))
    else:
        sys.exit("Uso: python -m app.wscpe dummy | consultar <ctg>")
