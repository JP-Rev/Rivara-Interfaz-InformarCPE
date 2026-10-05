"""Envío de mails por SMTP (Office 365 por omisión).

Misma forma que cpe_bolsatech.py: STARTTLS en el 587 y login con usuario y
contraseña. Con el puerto 465 se usa SSL directo.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from email.message import EmailMessage

from .config import config

log = logging.getLogger(__name__)


class ErrorCorreo(RuntimeError):
    pass


def problemas_correo() -> list[str]:
    """Lo que falta configurar para poder mandar mails (vacío si está todo)."""
    if not config.smtp_habilitado:
        return ["SMTP_HABILITADO no está en true"]
    faltan = [
        nombre for nombre, valor in (
            ("SMTP_SERVER", config.smtp_servidor),
            ("SMTP_USER", config.smtp_usuario),
            ("SMTP_PASS", config.smtp_clave),
            ("SMTP_TO", config.smtp_para),
        ) if not valor
    ]
    return [f"Falta {nombre} en el .env" for nombre in faltan]


def enviar(asunto: str, texto: str, html: str) -> None:
    """Manda el mail a SMTP_TO. Levanta ErrorCorreo con un mensaje legible."""
    problemas = problemas_correo()
    if problemas:
        raise ErrorCorreo("; ".join(problemas))

    mensaje = EmailMessage()
    mensaje["Subject"] = asunto
    mensaje["From"] = config.smtp_desde or config.smtp_usuario
    mensaje["To"] = ", ".join(config.smtp_para)
    mensaje.set_content(texto)
    mensaje.add_alternative(html, subtype="html")

    contexto = ssl.create_default_context()
    try:
        if config.smtp_puerto == 465:
            servidor = smtplib.SMTP_SSL(config.smtp_servidor, config.smtp_puerto, timeout=30, context=contexto)
        else:
            servidor = smtplib.SMTP(config.smtp_servidor, config.smtp_puerto, timeout=30)
        with servidor:
            if config.smtp_puerto != 465:
                servidor.starttls(context=contexto)
            servidor.login(config.smtp_usuario, config.smtp_clave)
            servidor.send_message(mensaje)
    except smtplib.SMTPAuthenticationError as error:
        detalle = error.smtp_error.decode(errors="replace") if isinstance(error.smtp_error, bytes) else str(error.smtp_error)
        # 5.7.139: Microsoft tiene desactivado el login SMTP con usuario y
        # contraseña para esa cuenta o para todo el tenant.
        pista = (" Microsoft tiene desactivada la autenticación SMTP para esa cuenta: "
                 "hay que habilitar 'SMTP autenticado' en el centro de administración de Microsoft 365.") \
            if "5.7.139" in detalle else ""
        raise ErrorCorreo(f"El servidor rechazó el usuario o la contraseña ({error.smtp_code}): {detalle}.{pista}") from error
    except (smtplib.SMTPException, OSError) as error:
        raise ErrorCorreo(f"No se pudo mandar el mail: {type(error).__name__}: {error}") from error
    log.info("Mail enviado a %s: %s", ", ".join(config.smtp_para), asunto)
