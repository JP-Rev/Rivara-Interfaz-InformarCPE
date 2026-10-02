"""WSAA: obtiene el ticket de acceso (token + sign) para el WS wscpe.

Firma un TRA con el certificado digital (CMS/PKCS#7) y lo canjea en LoginCms.
El ticket vale 12 horas y se guarda en disco: pedir uno nuevo mientras el
anterior sigue vigente da el error "El CEE ya posee un TA valido".
"""

from __future__ import annotations

import base64
import json
import logging
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.serialization import pkcs7

from .config import Credencial, config

SERVICIO = "wscpe"
MARGEN = timedelta(minutes=10)  # se renueva antes de que venza
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Ticket:
    token: str
    sign: str
    expira: datetime

    @property
    def vigente(self) -> bool:
        return datetime.now(timezone.utc) < self.expira - MARGEN


class ErrorWSAA(RuntimeError):
    pass


def _tra() -> bytes:
    # Sin microsegundos: WSAA es quisquilloso con el formato de las fechas.
    ahora = datetime.now(timezone.utc).astimezone().replace(microsecond=0)
    raiz = ET.Element("loginTicketRequest", version="1.0")
    cabecera = ET.SubElement(raiz, "header")
    # uniqueId tiene que crecer entre pedidos; el epoch en segundos alcanza.
    ET.SubElement(cabecera, "uniqueId").text = str(int(ahora.timestamp()))
    ET.SubElement(cabecera, "generationTime").text = (ahora - timedelta(minutes=10)).isoformat()
    ET.SubElement(cabecera, "expirationTime").text = (ahora + timedelta(minutes=10)).isoformat()
    ET.SubElement(raiz, "service").text = SERVICIO
    return ET.tostring(raiz, encoding="UTF-8", xml_declaration=True)


def _firmar(tra: bytes, credencial: Credencial) -> str:
    """Devuelve el TRA firmado como CMS en base64."""
    certificado = x509.load_pem_x509_certificate(credencial.certificado.read_bytes())
    clave = serialization.load_pem_private_key(credencial.clave.read_bytes(), password=None)
    cms = (
        pkcs7.PKCS7SignatureBuilder()
        .set_data(tra)
        .add_signer(certificado, clave, hashes.SHA256())
        .sign(serialization.Encoding.DER, [pkcs7.PKCS7Options.Binary])
    )
    return base64.b64encode(cms).decode("ascii")


def _login_cms(cms_b64: str) -> str:
    """Canjea el CMS en WSAA y devuelve el XML del loginTicketResponse."""
    sobre = f"""<?xml version="1.0" encoding="UTF-8"?>
<soapenv:Envelope xmlns:soapenv="http://schemas.xmlsoap.org/soap/envelope/"
                  xmlns:wsaa="http://wsaa.view.sua.dvadac.desein.afip.gov">
  <soapenv:Body>
    <wsaa:loginCms>
      <wsaa:in0>{cms_b64}</wsaa:in0>
    </wsaa:loginCms>
  </soapenv:Body>
</soapenv:Envelope>"""
    try:
        respuesta = requests.post(
            config.url_wsaa,
            data=sobre.encode("utf-8"),
            headers={"Content-Type": "text/xml; charset=utf-8", "SOAPAction": ""},
            timeout=config.timeout,
        )
    except requests.RequestException as error:
        raise ErrorWSAA(f"No se pudo conectar con WSAA: {error}") from error

    raiz = ET.fromstring(respuesta.text)
    retorno = next((e.text for e in raiz.iter() if e.tag.endswith("loginCmsReturn")), None)
    if retorno:
        return retorno

    detalle = next((e.text for e in raiz.iter() if e.tag.endswith("faultstring")), "")
    raise ErrorWSAA(f"WSAA rechazó el pedido (HTTP {respuesta.status_code}): {detalle or respuesta.text[:300]}")


def _parsear(xml_ta: str) -> Ticket:
    raiz = ET.fromstring(xml_ta)
    token = raiz.findtext(".//token") or ""
    sign = raiz.findtext(".//sign") or ""
    expiracion = raiz.findtext(".//expirationTime") or ""
    if not token or not sign:
        raise ErrorWSAA("El ticket de acceso no trae token o sign")
    return Ticket(token, sign, datetime.fromisoformat(expiracion))


def _archivo_cache(cuit: str) -> Path:
    return config.cache_dir / f"ta_{SERVICIO}_{cuit}.json"


def _leer_cache(cuit: str) -> Ticket | None:
    archivo = _archivo_cache(cuit)
    if not archivo.exists():
        return None
    try:
        datos = json.loads(archivo.read_text(encoding="utf-8"))
        ticket = Ticket(datos["token"], datos["sign"], datetime.fromisoformat(datos["expira"]))
    except (ValueError, KeyError) as error:
        log.warning("Ticket en caché ilegible (%s); se pide uno nuevo", error)
        return None
    return ticket if ticket.vigente else None


def _guardar_cache(cuit: str, ticket: Ticket) -> None:
    archivo = _archivo_cache(cuit)
    archivo.parent.mkdir(parents=True, exist_ok=True)
    temporal = archivo.with_suffix(".tmp")
    temporal.write_text(
        json.dumps({"token": ticket.token, "sign": ticket.sign, "expira": ticket.expira.isoformat()}),
        encoding="utf-8",
    )
    temporal.replace(archivo)
    archivo.chmod(0o600)


def autenticar(cuit: str) -> Ticket:
    """Ticket vigente para ese CUIT, de la caché o pedido a WSAA."""
    ticket = _leer_cache(cuit)
    if ticket:
        return ticket

    credencial = config.credencial(cuit)
    credencial.validar()
    log.info("Pidiendo ticket de acceso a WSAA (%s, CUIT %s)", config.entorno, cuit)
    ticket = _parsear(_login_cms(_firmar(_tra(), credencial)))
    _guardar_cache(cuit, ticket)
    return ticket
