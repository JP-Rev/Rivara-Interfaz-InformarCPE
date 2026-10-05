"""Configuración leída del entorno. Un solo lugar donde mirar qué se puede tocar."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
RAIZ = BASE_DIR.parent
PLANTILLA = BASE_DIR / "plantilla_visec.xlsx"

load_dotenv(RAIZ / ".env")

# WSAA y wscpe. Los de wscpe son los que declara el WSDL real
# (cpea-ws.afip.gob.ar/wscpe/services/soap?wsdl), no los del manual v2.0.5: el
# manual reemplazo "afip" por "arca" en todo el texto, pero el servicio sigue en
# el host de AFIP. cpea-ws.arca.gob.ar responde 200 vacio a cualquier pedido, y
# serviciosjava.afip.gob.ar (el de las bibliotecas viejas) da 404.
URLS = {
    "homologacion": {
        "wsaa": "https://wsaahomo.afip.gov.ar/ws/services/LoginCms",
        "wscpe": "https://cpea-ws-qaext.afip.gob.ar/wscpe/services/soap",
    },
    "produccion": {
        "wsaa": "https://wsaa.afip.gov.ar/ws/services/LoginCms",
        "wscpe": "https://cpea-ws.afip.gob.ar/wscpe/services/soap",
    },
}

# targetNamespace del WSDL. Va en los pedidos y en el SOAPAction.
NS_WSCPE = "https://serviciosjava.afip.gob.ar/wscpe/"


def _bool(nombre: str, defecto: bool = False) -> bool:
    return os.getenv(nombre, str(defecto)).strip().lower() in ("1", "true", "si", "sí", "yes")


def _mapa(nombre: str) -> dict[str, str]:
    """Lee un mapa 'a:b,c:d' del entorno."""
    salida: dict[str, str] = {}
    for par in os.getenv(nombre, "").split(","):
        if ":" in par:
            clave, valor = par.split(":", 1)
            salida[clave.strip()] = valor.strip()
    return salida


@dataclass(frozen=True)
class Credencial:
    """Certificado digital de un CUIT habilitado a consultar el WS."""

    cuit: str
    certificado: Path
    clave: Path

    def validar(self) -> None:
        for ruta in (self.certificado, self.clave):
            if not ruta.exists():
                raise FileNotFoundError(f"No existe {ruta} (CUIT {self.cuit})")


def _credenciales() -> list[Credencial]:
    """ARCA_CERTIFICADOS=cuit:/ruta/cert.crt:/ruta/clave.key,otro_cuit:...:...

    Admite varios CUIT porque una CPE solo la puede consultar quien intervino
    en ella: si una planta está a nombre de otra sociedad, hace falta su
    certificado.
    """
    credenciales = []
    for entrada in os.getenv("ARCA_CERTIFICADOS", "").split(","):
        partes = [p.strip() for p in entrada.split(":")]
        if len(partes) == 3 and all(partes):
            credenciales.append(Credencial(partes[0], Path(partes[1]), Path(partes[2])))
    return credenciales


@dataclass(frozen=True)
class Config:
    entorno: str = os.getenv("ARCA_ENTORNO", "homologacion").strip().lower()
    credenciales: list[Credencial] = field(default_factory=_credenciales)
    cache_dir: Path = Path(os.getenv("CACHE_DIR", "/data/cache"))
    timeout: int = int(os.getenv("ARCA_TIMEOUT", "60"))
    guardar_respuestas: bool = _bool("ARCA_GUARDAR_RESPUESTAS", True)

    # Mapeos propios de Visec, que no salen de ARCA
    grano_visec: dict[str, str] = field(default_factory=lambda: _mapa("MAPA_GRANO_VISEC"))
    tipo_movimiento: str = os.getenv("TIPO_MOVIMIENTO", "").strip()
    ultimo_almacenamiento: str = os.getenv("ULTIMO_ALMACENAMIENTO", "").strip()
    formato_numero_cpe: str = os.getenv("FORMATO_NUMERO_CPE", "{sucursal:05d}-{nro_orden:08d}")
    formato_fecha_hora: str = os.getenv("FORMATO_FECHA_HORA", "%d/%m/%Y %H:%M")
    formato_fecha: str = os.getenv("FORMATO_FECHA", "%d/%m/%Y")

    # Sesión compartida con el portal (cookie rivara_he, JWT HS256)
    jwt_secret: str = os.getenv("JWT_SECRET", "").strip()
    # El portal vive en el mismo host, en otro puerto. La URL se arma con el
    # nombre con el que entro el navegador (§6 del contrato: nada de hosts
    # hardcodeados), asi que funciona por IP y por nombre sin configurar nada.
    # PORTAL_URL solo hace falta si el portal esta en otra maquina.
    portal_puerto: int = int(os.getenv("PORTAL_PUERTO", "1003"))
    portal_url: str = os.getenv("PORTAL_URL", "").strip()
    auth_desactivada: bool = _bool("AUTH_DESACTIVADA", False)

    # Correo (Office 365 u otro SMTP). Mismos nombres que el .env de
    # cpe_bolsatech.py, asi se copian los valores tal cual.
    smtp_habilitado: bool = _bool("SMTP_HABILITADO", False)
    smtp_servidor: str = os.getenv("SMTP_SERVER", "smtp.office365.com").strip()
    smtp_puerto: int = int(os.getenv("SMTP_PORT", "587"))
    smtp_usuario: str = os.getenv("SMTP_USER", "").strip()
    smtp_clave: str = os.getenv("SMTP_PASS", "")
    smtp_desde: str = os.getenv("SMTP_FROM", "").strip()
    smtp_para: list[str] = field(default_factory=lambda: [
        c.strip() for c in os.getenv("SMTP_TO", "").split(",") if c.strip()
    ])

    # Aviso de vencimiento de los certificados de ARCA: un mail al entrar en
    # cada umbral (dias antes del vencimiento), una sola vez por umbral.
    cert_aviso_dias: list[int] = field(default_factory=lambda: sorted(
        {int(d) for d in os.getenv("CERT_AVISO_DIAS", "30,15,7,3,1").split(",") if d.strip().isdigit()},
        reverse=True,
    ))
    cert_chequeo_horas: int = int(os.getenv("CERT_CHEQUEO_HORAS", "12"))
    # La direccion de la app, para el link del mail: el aviso sale de un proceso
    # en segundo plano y no hay un navegador del que sacar el host.
    app_url: str = os.getenv("APP_URL", "").strip().rstrip("/")

    # ARCA mueve los endpoints cada tanto (y migro de afip.gob.ar a arca.gob.ar),
    # asi que las URL se pueden pisar desde el .env sin tocar el codigo.
    url_wsaa_override: str = os.getenv("ARCA_URL_WSAA", "").strip()
    url_wscpe_override: str = os.getenv("ARCA_URL_WSCPE", "").strip()
    ns_wscpe: str = os.getenv("ARCA_NS_WSCPE", "").strip() or NS_WSCPE

    @property
    def url_wsaa(self) -> str:
        return self.url_wsaa_override or URLS[self.entorno]["wsaa"]

    @property
    def url_wscpe(self) -> str:
        url = self.url_wscpe_override or URLS[self.entorno]["wscpe"]
        # Se le habla al endpoint, no al WSDL: si quedo "?wsdl" de una
        # configuracion anterior, se saca.
        return url.split("?", 1)[0]

    @property
    def cuits(self) -> list[str]:
        return [c.cuit for c in self.credenciales]

    def credencial(self, cuit: str) -> Credencial:
        for credencial in self.credenciales:
            if credencial.cuit == cuit:
                return credencial
        raise KeyError(f"No hay certificado configurado para el CUIT {cuit}")

    def validar(self) -> list[str]:
        """Devuelve la lista de problemas de configuración (vacía si está todo)."""
        problemas = []
        if self.entorno not in URLS:
            problemas.append(f"ARCA_ENTORNO debe ser 'homologacion' o 'produccion', no '{self.entorno}'")
        if not self.credenciales:
            problemas.append("Falta ARCA_CERTIFICADOS (formato cuit:/ruta/cert.crt:/ruta/clave.key)")
        for credencial in self.credenciales:
            try:
                credencial.validar()
            except FileNotFoundError as error:
                problemas.append(str(error))
        if not self.auth_desactivada and not self.jwt_secret:
            problemas.append("Falta JWT_SECRET (el mismo del portal) o AUTH_DESACTIVADA=true")
        return problemas


config = Config()
