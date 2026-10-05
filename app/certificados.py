"""Certificados digitales de ARCA: estado, reemplazo y aviso de vencimiento.

El reemplazo valida todo antes de tocar un archivo (que sea un certificado, que
la clave corresponda, que el CUIT sea el que se reemplaza y que no esté
vencido) y deja una copia de los anteriores en certs/respaldo, así un error se
deshace copiando de vuelta.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

from .config import Credencial, config
from .correo import ErrorCorreo, enviar

log = logging.getLogger(__name__)

TAMANO_MAXIMO = 64 * 1024  # un certificado o una clave pesan pocos kB


class ErrorCertificado(ValueError):
    pass


@dataclass
class Estado:
    cuit: str
    titular: str = ""
    vence: datetime | None = None
    error: str = ""

    @property
    def dias(self) -> int | None:
        if not self.vence:
            return None
        return (self.vence - datetime.now(timezone.utc)).days

    @property
    def nivel(self) -> str:
        """ok, aviso (dentro del primer umbral), vencido o error."""
        if self.error or self.dias is None:
            return "error"
        if self.dias < 0:
            return "vencido"
        umbral = max(config.cert_aviso_dias, default=30)
        return "aviso" if self.dias <= umbral else "ok"


# ---------------------------------------------------------------------------
# Lectura y validación
# ---------------------------------------------------------------------------

def _cargar_certificado(datos: bytes) -> x509.Certificate:
    try:
        return x509.load_pem_x509_certificate(datos)
    except ValueError:
        pass
    try:
        return x509.load_der_x509_certificate(datos)  # ARCA a veces lo da en DER
    except ValueError as error:
        raise ErrorCertificado("El archivo no es un certificado (.crt) válido") from error


def _cargar_clave(datos: bytes):
    try:
        return serialization.load_pem_private_key(datos, password=None)
    except TypeError as error:
        raise ErrorCertificado(
            "La clave privada tiene contraseña. La app necesita la clave sin contraseña: "
            "openssl rsa -in clave.key -out clave_sin_pass.key"
        ) from error
    except ValueError as error:
        raise ErrorCertificado("El archivo no es una clave privada (.key) válida") from error


def _cuit_de(certificado: x509.Certificate) -> str:
    """ARCA pone el CUIT en el serialNumber del sujeto: 'CUIT 30601191640'."""
    for atributo in certificado.subject.get_attributes_for_oid(NameOID.SERIAL_NUMBER):
        digitos = re.sub(r"\D", "", str(atributo.value))
        if len(digitos) == 11:
            return digitos
    return ""


def _titular_de(certificado: x509.Certificate) -> str:
    nombres = certificado.subject.get_attributes_for_oid(NameOID.ORGANIZATION_NAME) \
        or certificado.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    return str(nombres[0].value) if nombres else ""


def _corresponden(certificado: x509.Certificate, clave) -> bool:
    publica = lambda k: k.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return publica(certificado.public_key()) == publica(clave.public_key())


def estado(credencial: Credencial) -> Estado:
    try:
        certificado = _cargar_certificado(credencial.certificado.read_bytes())
    except FileNotFoundError:
        return Estado(credencial.cuit, error=f"No existe {credencial.certificado}")
    except ErrorCertificado as error:
        return Estado(credencial.cuit, error=str(error))
    return Estado(credencial.cuit, _titular_de(certificado), certificado.not_valid_after_utc)


def estados() -> list[Estado]:
    return [estado(c) for c in config.credenciales]


# ---------------------------------------------------------------------------
# Reemplazo
# ---------------------------------------------------------------------------

def _escribir(destino: Path, datos: bytes, permisos: int) -> None:
    temporal = destino.with_name(destino.name + ".nuevo")
    temporal.write_bytes(datos)
    temporal.chmod(permisos)
    temporal.replace(destino)  # atómico: nunca queda un archivo a medio escribir


def reemplazar(cuit: str, crt: bytes, key: bytes | None) -> Estado:
    """Valida el certificado nuevo y lo pone en lugar del actual.

    Sin `key` se usa la clave actual: es el caso de una renovación con la misma
    clave, donde ARCA solo entrega el .crt nuevo.
    """
    credencial = config.credencial(cuit)
    if len(crt) > TAMANO_MAXIMO or (key and len(key) > TAMANO_MAXIMO):
        raise ErrorCertificado("El archivo es demasiado grande para ser un certificado")

    certificado = _cargar_certificado(crt)
    cuit_nuevo = _cuit_de(certificado)
    if cuit_nuevo != cuit:
        raise ErrorCertificado(
            f"El certificado es del CUIT {cuit_nuevo or 'desconocido'}, no del {cuit}"
        )
    if certificado.not_valid_after_utc <= datetime.now(timezone.utc):
        raise ErrorCertificado(f"El certificado ya venció el {certificado.not_valid_after_utc:%d/%m/%Y}")

    if key:
        clave = _cargar_clave(key)
    else:
        try:
            clave = _cargar_clave(credencial.clave.read_bytes())
        except FileNotFoundError as error:
            raise ErrorCertificado("No hay una clave actual: subí también el .key") from error
    if not _corresponden(certificado, clave):
        raise ErrorCertificado(
            "El certificado no corresponde a la clave privada"
            + ("" if key else " actual: si lo pediste con una clave nueva, subí también el .key")
        )

    # Respaldo de lo que se reemplaza.
    respaldo = credencial.certificado.parent / "respaldo"
    respaldo.mkdir(exist_ok=True)
    marca = datetime.now().strftime("%Y%m%d-%H%M%S")
    for actual in [credencial.certificado] + ([credencial.clave] if key else []):
        if actual.exists():
            copia = respaldo / f"{actual.name}.{marca}"
            copia.write_bytes(actual.read_bytes())
            copia.chmod(0o600)

    _escribir(credencial.certificado, certificado.public_bytes(serialization.Encoding.PEM), 0o644)
    if key:
        _escribir(credencial.clave, key, 0o600)

    # El ticket de WSAA en caché salió del certificado anterior.
    (config.cache_dir / f"ta_wscpe_{cuit}.json").unlink(missing_ok=True)
    _olvidar_avisos(cuit)
    log.info("Certificado del CUIT %s reemplazado; vence el %s", cuit, certificado.not_valid_after_utc)
    return estado(credencial)


# ---------------------------------------------------------------------------
# Aviso de vencimiento
# ---------------------------------------------------------------------------

def _archivo_avisos() -> Path:
    return config.cache_dir / "avisos_certificados.json"


def _leer_avisos() -> dict:
    try:
        return json.loads(_archivo_avisos().read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        return {}


def _guardar_avisos(avisos: dict) -> None:
    archivo = _archivo_avisos()
    archivo.parent.mkdir(parents=True, exist_ok=True)
    archivo.write_text(json.dumps(avisos, indent=2), encoding="utf-8")


def _olvidar_avisos(cuit: str) -> None:
    avisos = _leer_avisos()
    if avisos.pop(cuit, None) is not None:
        _guardar_avisos(avisos)


def link_certificados() -> str:
    return f"{config.app_url}/certificados" if config.app_url else ""


def _mail_vencimiento(est: Estado) -> tuple[str, str, str]:
    link = link_certificados()
    if est.dias is not None and est.dias < 0:
        situacion = f"venció el {est.vence:%d/%m/%Y}"
        asunto = f"Certificado de ARCA VENCIDO — CUIT {est.cuit}"
    else:
        situacion = f"vence el {est.vence:%d/%m/%Y} (en {est.dias} días)"
        asunto = f"Certificado de ARCA por vencer en {est.dias} días — CUIT {est.cuit}"

    pasos = [
        "Generá un pedido nuevo (CSR) y subilo en ARCA, en Administración de Certificados Digitales.",
        "Descargá de ARCA el certificado nuevo (.crt).",
        "Entrá a la página de certificados y arrastrá el .crt. Si generaste una clave nueva, arrastrá también el .key.",
    ]
    texto = (
        f"El certificado digital de ARCA de {est.titular or est.cuit} (CUIT {est.cuit}) {situacion}.\n\n"
        "Sin un certificado vigente, la app no puede consultar las cartas de porte.\n\n"
        + "\n".join(f"{i}. {p}" for i, p in enumerate(pasos, 1))
        + (f"\n\nPágina para cargarlo: {link}\n" if link else
           "\n\n(Configurá APP_URL en el .env para que este mail traiga el link.)\n")
    )
    boton = (
        f'<p><a href="{escape(link)}" style="display:inline-block;padding:10px 20px;'
        'background:#3c5a32;color:#fff;border-radius:999px;text-decoration:none;font-weight:700">'
        "Cargar el certificado nuevo</a></p>"
    ) if link else "<p><em>Configurá APP_URL en el .env para que este mail traiga el link.</em></p>"
    html = f"""<div style="font-family:Arial,sans-serif;color:#23301F;max-width:560px">
<h2 style="color:#3c5a32">Informar CPE a Visec</h2>
<p>El certificado digital de ARCA de <strong>{escape(est.titular or est.cuit)}</strong>
(CUIT {est.cuit}) <strong>{situacion}</strong>.</p>
<p>Sin un certificado vigente, la app no puede consultar las cartas de porte.</p>
<ol>{''.join(f'<li>{escape(p)}</li>' for p in pasos)}</ol>
{boton}
</div>"""
    return asunto, texto, html


def chequear_y_avisar() -> list[str]:
    """Manda un mail por cada certificado que entró en un umbral nuevo.

    Devuelve lo que avisó. Guarda qué umbrales ya avisó por certificado (por
    CUIT y fecha de vencimiento), así no repite el mail en cada chequeo y, al
    reemplazar el certificado, el contador vuelve a cero solo.
    """
    avisos = _leer_avisos()
    enviados: list[str] = []
    for est in estados():
        if est.error or est.dias is None:
            log.warning("Certificado del CUIT %s: %s", est.cuit, est.error or "sin fecha")
            continue
        alcanzados = [u for u in config.cert_aviso_dias if est.dias <= u]
        if not alcanzados and est.dias >= 0:
            continue
        # El umbral más chico alcanzado; -1 si ya venció (se avisa una vez).
        umbral = -1 if est.dias < 0 else min(alcanzados)
        registro = avisos.get(est.cuit, {})
        clave_venc = est.vence.isoformat()
        if registro.get("vence") != clave_venc:
            registro = {"vence": clave_venc, "umbrales": []}
        if umbral in registro["umbrales"]:
            continue
        try:
            enviar(*_mail_vencimiento(est))
        except ErrorCorreo as error:
            log.error("No se pudo avisar el vencimiento del CUIT %s: %s", est.cuit, error)
            continue  # se reintenta en el próximo chequeo
        registro["umbrales"].append(umbral)
        avisos[est.cuit] = registro
        _guardar_avisos(avisos)
        enviados.append(f"{est.cuit}: {est.dias} días")
    return enviados
