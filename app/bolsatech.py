"""Informar las CPE de soja a BolsaTech (SIBIO), para el muestreo de
biotecnologías.

A diferencia de Visec y Albor, que solo arman un archivo, acá se **escribe en
un sistema externo**: un alta registrada no se deshace con un botón. Por eso:

- la pantalla muestra qué se va a informar antes de informar nada;
- el envío real es una acción aparte, nunca el efecto de una variable del
  `.env` que alguien se olvidó de cambiar;
- de cada CPE queda guardado qué pasó (`estado.json`), así una tanda repetida
  no vuelve a informar lo que ya estaba.

Son tres operaciones SOAP, con la contraseña en texto plano en el header
(WS-Security UsernameToken), que es lo que pide el servicio:

    consultarCartasDePorte      ¿ya está informada?      SOAP 1.2
    registrarCartasDePorte      el alta, devuelve trackId SOAP 1.2
    confirmarRecepcionDeTrackId confirma ese trackId      SOAP 1.1
"""

from __future__ import annotations

import json
import logging
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from xml.sax.saxutils import escape

import requests

from .config import config

log = logging.getLogger(__name__)

BOLSATECH_DIR = Path(__import__("os").getenv("BOLSATECH_DIR", "/data/bolsatech"))
ESTADO = BOLSATECH_DIR / "estado.json"
XML_DIR = BOLSATECH_DIR / "xml"

SOAP12 = "http://www.w3.org/2003/05/soap-envelope"
SOAP11 = "http://schemas.xmlsoap.org/soap/envelope/"
NS_CPE = "http://pod.waybillmanagement.ws.industrysystem.com.ar/"
NS_TRACK = "http://trackidmanagment.ws.industrysystem.com.ar/"
NS_WSSE = "http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd"
TIPO_CLAVE = ("http://docs.oasis-open.org/wss/2004/01/"
              "oasis-200401-wss-username-token-profile-1.0#PasswordText")

# Estados de cada CPE. Los dos primeros son finales: no se vuelve a intentar.
INFORMADA = "informada"
YA_ESTABA = "ya_estaba"
SIN_CONFIRMAR = "sin_confirmar"   # el alta salió, pero falta confirmar el trackId
ERROR = "error"
FINALES = {INFORMADA, YA_ESTABA}


class ErrorBolsaTech(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Los datos de una CPE, ya listos para el XML
# ---------------------------------------------------------------------------

@dataclass
class Cpe:
    ctg: str
    numero_carta_porte: str
    cuit_productor: str
    productor: str
    especie: str                 # código SAGPyA (23 = soja)
    establecimiento: str
    localidad_procedencia: str
    planta_oncca: str
    localidad_destino: str
    id_muestra: str
    kilos: str
    fecha: str                   # ISO, como lo pide el servicio
    cosecha: str = ""
    # Para mostrar en pantalla, no van al XML
    ingreso: str = ""
    sociedad: str = ""
    planta: str = ""
    especie_erp: str = ""
    errores: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errores

    @property
    def cuenta(self) -> str:
        """Qué credenciales usar. Lo decide el prefijo del id de muestra."""
        prefijo = config.bolsatech_prefijo_mliaudat
        return "mliaudat" if prefijo and self.id_muestra.startswith(prefijo) else "rivara"


def _texto(valor) -> str:
    if valor is None:
        return ""
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%dT%H:%M:%S")
    if isinstance(valor, float) and valor.is_integer():
        return str(int(valor))
    return str(valor).strip()


def _kilos(valor) -> str:
    try:
        numero = Decimal(_texto(valor).replace(",", "."))
    except InvalidOperation:
        return ""
    if numero <= 0:
        return ""
    return str(int(numero.quantize(Decimal("1"), rounding=ROUND_HALF_UP)))


def _cuit(valor: str) -> str:
    digitos = re.sub(r"\D", "", valor or "")
    return digitos if len(digitos) == 11 else ""


def armar(fila: dict, prefijo_muestra: str = "") -> Cpe:
    """Una fila de oracle.buscar_bolsatech() -> la CPE lista para informar.

    `prefijo_muestra` fuerza el prefijo del id de muestra para los ingresos que
    no lo tienen cargado en el ERP; vacío significa usar el de cada planta.
    """
    oncca = _texto(fila.get("planta_oncca"))
    planta = config.datos_planta(oncca)
    ctg = re.sub(r"\D", "", _texto(fila.get("ctg")))

    # numeroCartaPorte: sucursal interna + número interno en 8 posiciones.
    sucursal = _texto(fila.get("sucursal_cpe")) or "1"
    numero = _texto(fila.get("numero_cpe"))
    carta = f"{sucursal}{numero.zfill(8)}" if numero else ""

    id_muestra = _texto(fila.get("id_muestra"))
    if not id_muestra and ctg:
        id_muestra = f"{prefijo_muestra or planta.get('prefijo', '')}{ctg}"

    cpe = Cpe(
        ctg=ctg,
        numero_carta_porte=carta,
        cuit_productor=_cuit(_texto(fila.get("cuit_productor"))),
        productor=_texto(fila.get("productor")),
        especie=_texto(fila.get("especie_sagpya")) or config.bolsatech_especie_defecto,
        establecimiento=_texto(fila.get("establecimiento")),
        localidad_procedencia=_texto(fila.get("localidad_procedencia")),
        planta_oncca=oncca,
        localidad_destino=planta.get("localidad", ""),
        id_muestra=id_muestra,
        kilos=_kilos(fila.get("kilos_netos_secos")),
        fecha=_texto(fila.get("fecha_hora")),
        cosecha=_texto(fila.get("cosecha")),
        ingreso=f"{_texto(fila.get('punto_ingreso'))}-{_texto(fila.get('numero_ingreso'))}",
        sociedad=_texto(fila.get("sociedad")),
        planta=_texto(fila.get("planta")),
        especie_erp=_texto(fila.get("especie")) or _texto(fila.get("especie_codigo")),
    )
    cpe.errores = _validar(cpe)
    return cpe


def _validar(cpe: Cpe) -> list[str]:
    errores = []
    if len(cpe.ctg) != 11:
        errores.append(f"CTG inválido: «{cpe.ctg}»")
    if not cpe.numero_carta_porte.isdigit() or len(cpe.numero_carta_porte) != 9:
        errores.append(
            "El número de carta de porte tiene que ser de 9 dígitos "
            f"(sucursal + número interno): «{cpe.numero_carta_porte}»")
    elif cpe.numero_carta_porte == cpe.ctg:
        errores.append("El número de carta de porte no puede ser igual al CTG")
    if not cpe.cuit_productor:
        errores.append("CUIT del productor inválido")
    for valor, nombre in (
        (cpe.productor, "nombre del productor"),
        (cpe.establecimiento, "establecimiento"),
        (cpe.localidad_procedencia, "localidad de procedencia"),
        (cpe.planta_oncca, "número ONCCA de la planta"),
        (cpe.localidad_destino, "localidad de destino"),
        (cpe.id_muestra, "identificador de muestra"),
        (cpe.especie, "código de especie"),
    ):
        if not valor:
            errores.append(f"Falta el {nombre}")
    if not cpe.kilos:
        errores.append("Los kilos netos secos tienen que ser mayores que cero")
    if not cpe.fecha:
        errores.append("Falta la fecha del movimiento")
    return errores


def destinatario(planta_oncca: str) -> tuple[str, str]:
    """CUIT y razón social de la sociedad dueña de la planta de destino."""
    for entrada in config.bolsatech_destinatarios.split(","):
        partes = [p.strip() for p in entrada.split(":")]
        if len(partes) == 3 and partes[0] in (planta_oncca, "*"):
            return partes[1], partes[2]
    return "", ""


# ---------------------------------------------------------------------------
# Los sobres SOAP
# ---------------------------------------------------------------------------

def _seguridad(cuenta: str, prefijo: str) -> str:
    usuario = config.bolsatech_cuentas.get(cuenta, "")
    clave = config.bolsatech_claves.get(cuenta, "")
    if not usuario or not clave:
        raise ErrorBolsaTech(f"Faltan las credenciales de BolsaTech de la cuenta «{cuenta}».")
    return (f'<wsse:Security {prefijo}:mustUnderstand="1"><wsse:UsernameToken>'
            f"<wsse:Username>{escape(usuario)}</wsse:Username>"
            f'<wsse:Password Type="{TIPO_CLAVE}">{escape(clave)}</wsse:Password>'
            "</wsse:UsernameToken></wsse:Security>")


def xml_consulta(cpe: Cpe) -> str:
    identificador = escape(f"{cpe.numero_carta_porte}|{cpe.ctg}")
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP12}" xmlns:t="{NS_CPE}" xmlns:wsse="{NS_WSSE}">'
            f"<s:Header>{_seguridad(cpe.cuenta, 's')}</s:Header><s:Body>"
            "<t:consultarCartasDePorte><parametros><numerosCartaPorte>"
            f"<numeroCartaPorte>{identificador}</numeroCartaPorte>"
            "</numerosCartaPorte></parametros></t:consultarCartasDePorte>"
            "</s:Body></s:Envelope>")


def xml_registro(cpe: Cpe) -> str:
    if not cpe.ok:
        raise ErrorBolsaTech("; ".join(cpe.errores))
    cuit_destino, razon_destino = destinatario(cpe.planta_oncca)
    if not cuit_destino:
        raise ErrorBolsaTech(
            f"No hay destinatario configurado para la planta ONCCA {cpe.planta_oncca}. "
            "Revisá BOLSATECH_DESTINATARIOS en el .env.")
    cosecha = (f"<cosecha>{escape(cpe.cosecha)}</cosecha>" if cpe.cosecha else "")
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP12}" xmlns:t="{NS_CPE}" xmlns:wsse="{NS_WSSE}">'
            f"<s:Header>{_seguridad(cpe.cuenta, 's')}</s:Header><s:Body>"
            "<t:registrarCartasDePorte><cartasDePorte><cartaDePorteTransporteAutomotor>"
            f"<numeroCartaPorte>{escape(cpe.numero_carta_porte)}</numeroCartaPorte>"
            "<intervinientes>"
            f"<titular><cuit>{cpe.cuit_productor}</cuit>"
            f"<razonSocial>{escape(cpe.productor)}</razonSocial></titular>"
            "<intermediario><cuit/><razonSocial/></intermediario>"
            f"<remitenteComercial><cuit>{cpe.cuit_productor}</cuit>"
            f"<razonSocial>{escape(cpe.productor)}</razonSocial></remitenteComercial>"
            "<corredor><cuit/><razonSocial/></corredor>"
            "<representante><cuit/><razonSocial/></representante>"
            f"<destinatario><cuit>{escape(cuit_destino)}</cuit>"
            f"<razonSocial>{escape(razon_destino)}</razonSocial></destinatario>"
            f"<destino><cuit>{escape(cuit_destino)}</cuit></destino>"
            "</intervinientes><granosTransportados>"
            f"<codigoEspecie>{escape(cpe.especie)}</codigoEspecie>"
            f"<establecimiento>{escape(cpe.establecimiento)}</establecimiento>"
            f"<codigoLocalidadProcedencia>{escape(cpe.localidad_procedencia)}</codigoLocalidadProcedencia>"
            "</granosTransportados><destinoGranos>"
            f"<numeroPlantaDestino>{escape(cpe.planta_oncca)}</numeroPlantaDestino>"
            f"<codigoLocalidadDestino>{escape(cpe.localidad_destino)}</codigoLocalidadDestino>"
            f"</destinoGranos>{cosecha}<numeroContrato/>"
            f"<ctg>{escape(cpe.ctg)}</ctg><muestreoYPesaje>"
            f"<fechaHoraConfirmacion>{escape(cpe.fecha)}</fechaHoraConfirmacion>"
            f"<idMuestra>{escape(cpe.id_muestra)}</idMuestra>"
            f"<kilosNetosSecos>{escape(cpe.kilos)}</kilosNetosSecos>"
            "</muestreoYPesaje></cartaDePorteTransporteAutomotor>"
            "</cartasDePorte></t:registrarCartasDePorte></s:Body></s:Envelope>")


def xml_confirmacion(cpe: Cpe, track_id: str) -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>'
            f'<s:Envelope xmlns:s="{SOAP11}" xmlns:t="{NS_TRACK}" xmlns:wsse="{NS_WSSE}">'
            f"<s:Header>{_seguridad(cpe.cuenta, 's')}</s:Header><s:Body>"
            "<t:confirmarRecepcionDeTrackId><confirmacionesDeRecepcionDeTrackId>"
            f"<confirmacionDeRecepcionDeTrackId><trackId>{escape(track_id)}</trackId>"
            "</confirmacionDeRecepcionDeTrackId>"
            "</confirmacionesDeRecepcionDeTrackId></t:confirmarRecepcionDeTrackId>"
            "</s:Body></s:Envelope>")


# ---------------------------------------------------------------------------
# Las respuestas
# ---------------------------------------------------------------------------

def _raiz(texto: str) -> ET.Element:
    try:
        return ET.fromstring(texto)
    except ET.ParseError as error:
        raise ErrorBolsaTech(f"BolsaTech devolvió algo que no es XML: {error}") from error


def _nombre(elemento: ET.Element) -> str:
    return elemento.tag.split("}")[-1]


def _todos(raiz: ET.Element, nombre: str) -> list[ET.Element]:
    return [e for e in raiz.iter() if _nombre(e) == nombre]


def _valor(elemento: ET.Element, nombre: str) -> str:
    for e in elemento.iter():
        if _nombre(e) == nombre:
            return (e.text or "").strip()
    return ""


def _revisar_fault(raiz: ET.Element) -> None:
    faults = _todos(raiz, "Fault")
    if not faults:
        return
    fault = faults[0]
    codigo = _valor(fault, "faultcode") or _valor(fault, "Value") or "sin código"
    detalle = (_valor(fault, "faultstring") or _valor(fault, "Text")
               or _valor(fault, "descripcion") or "sin descripción")
    raise ErrorBolsaTech(f"BolsaTech rechazó el pedido ({codigo}): {detalle}")


def _enviar(url: str, xml: str, accion: str, soap12: bool) -> str:
    cabeceras = {
        "Content-Type": (f'application/soap+xml; charset=utf-8; action="{accion}"'
                         if soap12 else "text/xml; charset=utf-8"),
        "Accept": "application/soap+xml, text/xml",
        "User-Agent": "RivaraInformarCPE/1.0",
    }
    if not soap12:
        cabeceras["SOAPAction"] = accion
    try:
        respuesta = requests.post(url, data=xml.encode("utf-8"), headers=cabeceras,
                                  timeout=config.bolsatech_timeout)
    except requests.RequestException as error:
        raise ErrorBolsaTech(f"No se pudo contactar a BolsaTech: {error}") from error
    if respuesta.status_code >= 300:
        _revisar_fault(_raiz(respuesta.text))
        raise ErrorBolsaTech(f"BolsaTech respondió HTTP {respuesta.status_code}")
    return respuesta.text


def _guardar_xml(nombre: str, ctg: str, contenido: str) -> None:
    """Deja el XML enviado y el recibido, que es lo único que queda si hay que
    reclamar algo."""
    try:
        XML_DIR.mkdir(parents=True, exist_ok=True)
        marca = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        (XML_DIR / f"{nombre}_{ctg}_{marca}.xml").write_text(contenido, encoding="utf-8")
    except OSError as error:
        log.warning("No se pudo guardar el XML %s: %s", nombre, error)


# ---------------------------------------------------------------------------
# Las tres operaciones
# ---------------------------------------------------------------------------

def ya_informada(cpe: Cpe) -> bool:
    xml = xml_consulta(cpe)
    _guardar_xml("consulta", cpe.ctg, xml)
    respuesta = _enviar(config.bolsatech_url, xml, "consultarCartasDePorte", True)
    _guardar_xml("consulta_respuesta", cpe.ctg, respuesta)
    raiz = _raiz(respuesta)
    _revisar_fault(raiz)
    return bool(_todos(raiz, "cartaPorteTransporteAutomotor"))


def registrar(cpe: Cpe) -> dict:
    xml = xml_registro(cpe)
    _guardar_xml("registro", cpe.ctg, xml)
    respuesta = _enviar(config.bolsatech_url, xml, "registrarCartasDePorte", True)
    _guardar_xml("registro_respuesta", cpe.ctg, respuesta)
    raiz = _raiz(respuesta)
    _revisar_fault(raiz)
    errores = []
    for error in _todos(raiz, "error"):
        codigo, detalle = _valor(error, "codigo"), _valor(error, "descripcion")
        if codigo or detalle:
            errores.append({"codigo": codigo, "descripcion": detalle})
    return {"track_id": _valor(raiz, "trackId"), "errores": errores}


def confirmar(cpe: Cpe, track_id: str) -> bool:
    if not config.bolsatech_url_track:
        return False
    xml = xml_confirmacion(cpe, track_id)
    _guardar_xml("confirmacion", cpe.ctg, xml)
    respuesta = _enviar(config.bolsatech_url_track, xml,
                        "confirmarRecepcionDeTrackId", False)
    _guardar_xml("confirmacion_respuesta", cpe.ctg, respuesta)
    raiz = _raiz(respuesta)
    _revisar_fault(raiz)
    return track_id in [_valor(e, "trackId")
                        for e in _todos(raiz, "confirmacionDeRecepcionDeTrackId")]


# ---------------------------------------------------------------------------
# Qué se informó: reemplaza al cpe_control.csv del script
# ---------------------------------------------------------------------------

def estado() -> dict[str, dict]:
    if not ESTADO.is_file():
        return {}
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        log.warning("No se pudo leer el estado de BolsaTech: %s", error)
        return {}


def _guardar_estado(datos: dict) -> None:
    BOLSATECH_DIR.mkdir(parents=True, exist_ok=True)
    temporal = ESTADO.with_suffix(".tmp")
    temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8")
    temporal.replace(ESTADO)


def anotar(cpe: Cpe, situacion: str, track_id: str = "", detalle: str = "") -> None:
    datos = estado()
    anterior = datos.get(cpe.ctg, {})
    datos[cpe.ctg] = {
        "estado": situacion,
        "numero_carta_porte": cpe.numero_carta_porte,
        "id_muestra": cpe.id_muestra,
        "cuenta": cpe.cuenta,
        "track_id": track_id or anterior.get("track_id", ""),
        "detalle": detalle,
        "intentos": int(anterior.get("intentos", 0)) + 1,
        "cuando": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    _guardar_estado(datos)


def informadas() -> set[str]:
    """Los CTG que ya no hay que volver a informar."""
    return {ctg for ctg, d in estado().items() if d.get("estado") in FINALES}


# ---------------------------------------------------------------------------
# La tanda: lo que se consultó y está esperando confirmación para enviarse
# ---------------------------------------------------------------------------

TANDAS = BOLSATECH_DIR / "tandas"
RETENCION_TANDAS = 2  # días


def guardar_tanda(filas: list[dict], prefijo: str, faltantes: list[str]) -> str:
    import uuid
    from datetime import timedelta

    TANDAS.mkdir(parents=True, exist_ok=True)
    limite = datetime.now() - timedelta(days=RETENCION_TANDAS)
    for vieja in TANDAS.glob("*.json"):
        if datetime.fromtimestamp(vieja.stat().st_mtime) < limite:
            vieja.unlink(missing_ok=True)
    tanda = uuid.uuid4().hex
    (TANDAS / f"{tanda}.json").write_text(
        json.dumps({"filas": filas, "prefijo": prefijo, "faltantes": faltantes},
                   ensure_ascii=False, default=str),
        encoding="utf-8")
    return tanda


def leer_tanda(tanda: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{32}", tanda or ""):
        return None
    ruta = TANDAS / f"{tanda}.json"
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.is_file() else None


def informar(cpe: Cpe) -> tuple[str, str, str]:
    """Informa una CPE. Devuelve (estado, track_id, detalle).

    Primero pregunta si ya está: informar dos veces la misma CPE es un error
    que después hay que ir a arreglar a mano del otro lado.
    """
    if ya_informada(cpe):
        anotar(cpe, YA_ESTABA, detalle="Ya estaba informada en BolsaTech")
        return YA_ESTABA, "", "Ya estaba informada"

    resultado = registrar(cpe)
    track_id = str(resultado.get("track_id") or "")
    errores = resultado.get("errores") or []

    if errores:
        detalle = " | ".join(f"{e.get('codigo', '')}: {e.get('descripcion', '')}"
                             for e in errores)
        # El servicio avisa así cuando el CTG ya existe; no es un error nuestro.
        if any("YA EXISTE UNA CARTA DE PORTE CON CTG" in str(e.get("descripcion", "")).upper()
               for e in errores):
            anotar(cpe, YA_ESTABA, track_id, detalle)
            return YA_ESTABA, track_id, detalle
        anotar(cpe, ERROR, track_id, detalle)
        return ERROR, track_id, detalle

    if not track_id:
        anotar(cpe, ERROR, detalle="El alta no devolvió trackId")
        return ERROR, "", "El alta no devolvió trackId"

    if confirmar(cpe, track_id):
        anotar(cpe, INFORMADA, track_id)
        return INFORMADA, track_id, ""
    detalle = ("Falta BOLSATECH_TRACK_URL en el .env"
               if not config.bolsatech_url_track else "BolsaTech no confirmó el trackId")
    anotar(cpe, SIN_CONFIRMAR, track_id, detalle)
    return SIN_CONFIRMAR, track_id, detalle
