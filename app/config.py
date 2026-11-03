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

    # Oracle del ERP (SoftCereal). Lectura de los ingresos a planta por CTG.
    # ORACLE_DSN tiene la forma host:puerto/service_name.
    # ORACLE_ESQUEMAS lista los esquemas a consultar: uno por sociedad, con el
    # nombre que se muestra en pantalla. Una sola conexion los lee a todos.
    oracle_dsn: str = os.getenv("ORACLE_DSN", "").strip()
    oracle_usuario: str = os.getenv("ORACLE_USUARIO", "").strip()
    oracle_clave: str = os.getenv("ORACLE_PASSWORD", "")
    oracle_esquemas: dict[str, str] = field(default_factory=lambda: _mapa("ORACLE_ESQUEMAS"))
    # Carpeta del Instant Client. Hace falta el modo "thick" porque la base es
    # 11.2 y el modo "thin" de oracledb necesita 12.1 o posterior.
    oracle_lib_dir: str = os.getenv("ORACLE_LIB_DIR", "").strip()

    # BolsaTech (SIBIO): se informan las CPE de soja para el muestreo de
    # biotecnologías. A diferencia de Visec y Albor, acá se escribe en un
    # sistema externo, así que el envío real es siempre una acción explícita.
    bolsatech_url: str = os.getenv("BOLSATECH_URL", "").strip()
    bolsatech_url_track: str = os.getenv("BOLSATECH_TRACK_URL", "").strip()
    bolsatech_timeout: int = int(os.getenv("BOLSATECH_TIMEOUT", "60"))
    # Dos cuentas: la de Rivara y la de MLiaudat. Cuál se usa lo decide el
    # prefijo del id de muestra (779... va por MLiaudat).
    bolsatech_cuentas: dict[str, str] = field(default_factory=lambda: {
        "rivara": os.getenv("BOLSATECH_USUARIO_RIVARA", "ws_rivara").strip(),
        "mliaudat": os.getenv("BOLSATECH_USUARIO_MLIAUDAT", "ws_mliaudat").strip(),
    })
    bolsatech_claves: dict[str, str] = field(default_factory=lambda: {
        "rivara": os.getenv("BOLSATECH_PASSWORD_RIVARA", ""),
        "mliaudat": os.getenv("BOLSATECH_PASSWORD_MLIAUDAT", ""),
    })
    bolsatech_prefijo_mliaudat: str = os.getenv("BOLSATECH_PREFIJO_MLIAUDAT", "779").strip()
    # Especie que se informa cuando el ERP no trae el código SAGPyA. 23 es soja.
    bolsatech_especie_defecto: str = os.getenv("BOLSATECH_ESPECIE_DEFECTO", "23").strip()
    # Las especies del ERP que se informan a BolsaTech (todas variantes de soja).
    bolsatech_especies: list[str] = field(default_factory=lambda: [
        e.strip() for e in os.getenv("BOLSATECH_ESPECIES", "08,14,20,24,38").split(",") if e.strip()
    ])
    # Por planta (número ONCCA): localidad de destino, puerto y prefijo del id
    # de muestra. Formato ONCCA:localidad:puerto:prefijo, separados por coma.
    # El primero que no tiene número ONCCA es el que se usa por omisión.
    bolsatech_plantas: str = os.getenv(
        "BOLSATECH_PLANTAS", "525045:1170:7790:7790,*:252:5150:5140").strip()
    # Corrida automática: busca por fecha e informa sola, como hacía la tarea
    # programada. Apagada por omisión: informar escribe en un sistema externo,
    # así que que corra sola tiene que ser una decisión, no un descuido.
    bolsatech_automatico: bool = _bool("BOLSATECH_AUTOMATICO", False)
    bolsatech_cada_horas: int = int(os.getenv("BOLSATECH_CADA_HORAS", "6"))
    # Desde cuándo buscar la primera vez (después manda la última corrida).
    bolsatech_fecha_inicial: str = os.getenv("BOLSATECH_FECHA_INICIAL", "").strip()
    # Se vuelve un poco para atrás, por si algo se cargó con la fecha corrida.
    bolsatech_solapamiento: int = int(os.getenv("BOLSATECH_SOLAPAMIENTO_MINUTOS", "5"))
    # Tope por corrida, para que un error no se multiplique por mil.
    bolsatech_limite: int = int(os.getenv("BOLSATECH_LIMITE", "200"))

    # Quién recibe la mercadería, por planta: ONCCA:CUIT:razón social.
    # '*' es la sociedad por omisión.
    bolsatech_destinatarios: str = os.getenv(
        "BOLSATECH_DESTINATARIOS",
        "525045:30716318989:Pradera Natural S.A.,*:30601191640:Rivara S.A.").strip()

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
    def plantas_bolsatech(self) -> dict[str, dict[str, str]]:
        """ONCCA -> {localidad, puerto, prefijo}. '*' es el valor por omisión."""
        salida: dict[str, dict[str, str]] = {}
        for entrada in self.bolsatech_plantas.split(","):
            partes = [p.strip() for p in entrada.split(":")]
            if len(partes) == 4 and all(partes):
                salida[partes[0]] = {"localidad": partes[1], "puerto": partes[2],
                                     "prefijo": partes[3]}
        return salida

    def datos_planta(self, oncca: str) -> dict[str, str]:
        plantas = self.plantas_bolsatech
        return plantas.get(str(oncca or "").strip(), plantas.get("*", {}))

    @property
    def bolsatech_listo(self) -> bool:
        return not self.problemas_bolsatech()

    def problemas_bolsatech(self) -> list[str]:
        faltan = []
        if not self.bolsatech_url:
            faltan.append("BOLSATECH_URL")
        if not self.bolsatech_claves.get("rivara"):
            faltan.append("BOLSATECH_PASSWORD_RIVARA")
        if not self.bolsatech_claves.get("mliaudat"):
            faltan.append("BOLSATECH_PASSWORD_MLIAUDAT")
        problemas = [f"Falta {f} en el .env" for f in faltan]
        if not self.bolsatech_url_track:
            problemas.append(
                "Falta BOLSATECH_TRACK_URL: sin esa URL las altas quedan "
                "registradas pero sin confirmar el trackId"
            )
        return problemas

    @property
    def oracle_listo(self) -> bool:
        return bool(self.oracle_dsn and self.oracle_usuario and self.oracle_esquemas)

    def problemas_oracle(self) -> list[str]:
        faltan = []
        if not self.oracle_dsn:
            faltan.append("ORACLE_DSN (host:puerto/service_name)")
        if not self.oracle_usuario:
            faltan.append("ORACLE_USUARIO")
        if not self.oracle_clave:
            faltan.append("ORACLE_PASSWORD")
        if not self.oracle_esquemas:
            faltan.append("ORACLE_ESQUEMAS (formato ESQUEMA:Nombre,OTRO:Nombre)")
        return [f"Falta {f} en el .env" for f in faltan]

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
