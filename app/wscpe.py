"""Cliente del WS de Carta de Porte Electrónica (wscpe) de ARCA.

Solo se usa la consulta: `consultarCPEAutomotor` por número de CTG. La app no
autoriza, anula ni modifica nada, así que no hay forma de alterar una CPE.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from zeep import Client, Settings
from zeep.exceptions import Error as ZeepError
from zeep.helpers import serialize_object
from zeep.transports import Transport

from .config import config
from .wsaa import ErrorWSAA, autenticar

log = logging.getLogger(__name__)

# Errores de ARCA que significan "este CUIT no intervino en esta CPE": con
# varios certificados configurados vale la pena reintentar con otro.
CODIGOS_NO_INTERVINIENTE = {"1401", "1402", "2001"}
TEXTOS_NO_INTERVINIENTE = ("no es interviniente", "no participa", "no se encuentra autorizado")


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


_cliente: Client | None = None


def cliente() -> Client:
    """Cliente SOAP, construido una sola vez (bajar el WSDL es lento)."""
    global _cliente
    if _cliente is None:
        log.info("Descargando WSDL de wscpe (%s)", config.entorno)
        # strict=False: si ARCA agrega un campo nuevo, la respuesta no se cae.
        _cliente = Client(
            config.url_wscpe,
            settings=Settings(strict=False, xml_huge_tree=True),
            transport=Transport(timeout=config.timeout, operation_timeout=config.timeout),
        )
    return _cliente


def _errores_de(respuesta: dict) -> list[str]:
    """Aplana la lista de errores que devuelve ARCA."""
    mensajes = []
    for entrada in respuesta.get("errores") or []:
        error = entrada.get("error") if isinstance(entrada, dict) else entrada
        for item in error if isinstance(error, list) else [error]:
            if isinstance(item, dict):
                mensajes.append(f"{item.get('codigo', '')}: {item.get('descripcion', '')}".strip(": "))
    return mensajes


def _es_no_interviniente(errores: list[str]) -> bool:
    for mensaje in errores:
        codigo = mensaje.split(":", 1)[0].strip()
        if codigo in CODIGOS_NO_INTERVINIENTE:
            return True
        if any(texto in mensaje.lower() for texto in TEXTOS_NO_INTERVINIENTE):
            return True
    return False


def _guardar_respuesta(ctg: str, datos: dict) -> None:
    """Guarda la respuesta cruda. Sirve para completar el mapeo y para auditar."""
    if not config.guardar_respuestas:
        return
    carpeta = config.cache_dir / "respuestas"
    carpeta.mkdir(parents=True, exist_ok=True)
    archivo = carpeta / f"cpe_{ctg}_{datetime.now():%Y%m%d_%H%M%S}.json"
    archivo.write_text(json.dumps(datos, indent=2, ensure_ascii=False, default=str), encoding="utf-8")


def _consultar_con(ctg: str, cuit: str) -> tuple[dict | None, list[str]]:
    ticket = autenticar(cuit)
    respuesta = cliente().service.consultarCPEAutomotor(
        auth={"token": ticket.token, "sign": ticket.sign, "cuitRepresentada": cuit},
        solicitud={"nroCTG": int(ctg), "cuitSolicitante": cuit},
    )
    datos = serialize_object(respuesta, dict) or {}
    cuerpo = datos.get("respuesta", datos) or {}
    errores = _errores_de(cuerpo)
    if cuerpo.get("cabecera"):
        _guardar_respuesta(ctg, cuerpo)
        return cuerpo, errores
    return None, errores or ["ARCA no devolvió datos de la CPE"]


def consultar(ctg: str) -> Consulta:
    """Consulta un CTG probando los certificados configurados."""
    cuits = config.cuits
    if not cuits:
        return Consulta(ctg, errores=["No hay certificados configurados (ARCA_CERTIFICADOS)"])

    ultimos_errores: list[str] = []
    for indice, cuit in enumerate(cuits):
        try:
            cpe, errores = _consultar_con(ctg, cuit)
        except (ErrorWSAA, ZeepError, OSError, ValueError) as error:
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
