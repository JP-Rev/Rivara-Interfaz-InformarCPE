"""Informar CPE a VISEC por su API de trazabilidad B2B.

El módulo del XLSX (visec.py) sigue igual y no se toca. Este reusa lo que ese
ya resuelve: arma cada CPE con `visec.armar_fila`, la misma función que llena
la planilla, y solo convierte esas 25 columnas al JSON que pide la API. La API
es, campo por campo, la misma planilla.

De dónde sale cada cosa:

    Oracle  -> qué CTG informar, y lo que ARCA no tiene (fecha del movimiento,
               peso que entró a stock, kilos estimados, RENSPA). Las mismas
               columnas que scripts/ctg_soja.py.
    ARCA    -> el resto de la CPE (intervinientes, RUCA, producto, campaña...).
    VISEC   -> recibe el JSON y lo procesa en forma asincrónica: devuelve un
               número de proceso, y el resultado de cada CTG se consulta después.

Solo se informan CPE que ARCA ya dio por confirmadas o descargadas (CN, DD): con
el XLSX la persona ve el aviso de "pesos no definitivos" y decide; acá nadie
mira, así que una CPE todavía activa se saltea y se reintenta en la próxima
corrida hasta que se confirme.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from . import visec
from .config import config
from .wscpe import Consulta

log = logging.getLogger(__name__)

VISEC_API_DIR = Path(os.getenv("VISEC_API_DIR", "/data/visec_api"))
ESTADO = VISEC_API_DIR / "estado.json"
RESPUESTAS_DIR = VISEC_API_DIR / "respuestas"
ULTIMA_AUTOMATICA = VISEC_API_DIR / "ultima_automatica.json"
FORMATO = "%Y-%m-%d %H:%M:%S"

# Argentina no tiene horario de verano: el desfase es fijo. Va explícito en
# fechaHoraMovimiento para que del otro lado no haya que adivinar la zona.
ZONA = "-03:00"

RUTA_ALTA = "/ProcesoImportacionJsonCartaPorte"
RUTA_PROCESO = "/ConsultarProcesoImportacionCartaPorte"
RUTA_ESTADO_CTG = "/ConsultaStock/ConsultarEstadoCTG"

# Estados de cada CTG en estado.json
REGISTRADA = "registrada"          # VISEC la registró. Final.
RECHAZADA = "rechazada"            # VISEC dijo que no. No se reintenta sola.
ENVIADA = "enviada"                # está en un proceso, falta el resultado
ESPERANDO_ARCA = "esperando_arca"  # ARCA todavía no la confirmó: se reintenta
FALTAN_DATOS = "faltan_datos"      # le falta algo obligatorio: se reintenta
ERROR = "error"                    # algo técnico (red, ARCA caído): se reintenta

# Lo que no se vuelve a mandar. RECHAZADA está acá a propósito: si VISEC
# rechazó por un RENSPA que no tiene dado de alta, mandarla cada 15 minutos no
# lo arregla. Se reintenta a mano desde la pantalla, una vez corregido.
NO_REINTENTAR = {REGISTRADA, RECHAZADA, ENVIADA}

# El servicio no documenta los códigos de estado del detalle, así que una
# descripción con estas palabras se toma como rechazo aunque no traiga errores.
PALABRAS_DE_RECHAZO = re.compile(r"error|rechaz|inv[aá]lid|anulad", re.I)


class ErrorVisecApi(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# Token: OAuth2 client credentials, con Basic auth. Vive 8 horas.
# ---------------------------------------------------------------------------

_token: dict[str, Any] = {"valor": "", "vence": datetime.min, "ambiente": ""}
_candado_token = threading.Lock()


def token(renovar: bool = False) -> str:
    with _candado_token:
        vigente = (_token["valor"] and datetime.now() < _token["vence"]
                   and _token["ambiente"] == config.visec_api_ambiente)
        if vigente and not renovar:
            return _token["valor"]
        problemas = config.problemas_visec_api()
        if problemas:
            raise ErrorVisecApi("; ".join(problemas))
        urls = config.visec_api_urls
        try:
            r = requests.post(
                urls["token"],
                data={"grant_type": "client_credentials", "scope": urls["scope"]},
                auth=(config.visec_api_client_id, config.visec_api_client_secret),
                timeout=config.visec_api_timeout,
            )
        except requests.RequestException as error:
            raise ErrorVisecApi(f"No se pudo pedir el token a VISEC: {error}") from error
        if r.status_code != 200:
            raise ErrorVisecApi(
                f"VISEC no dio el token (HTTP {r.status_code}): {r.text[:300]}")
        try:
            datos = r.json()
        except ValueError as error:
            raise ErrorVisecApi("VISEC devolvió algo que no es JSON al pedir el token") from error
        valor = datos.get("access_token") or ""
        if not valor:
            raise ErrorVisecApi("La respuesta del token no trae access_token")
        vida = int(datos.get("expires_in") or 3600)
        # Se renueva cinco minutos antes, para no usar uno que vence en el camino.
        _token.update(valor=valor, ambiente=config.visec_api_ambiente,
                      vence=datetime.now() + timedelta(seconds=max(vida - 300, 60)))
        log.info("Token de VISEC (%s) renovado, vence en %s s", config.visec_api_ambiente, vida)
        return valor


def _llamar(ruta: str, cuerpo: dict) -> tuple[int, Any]:
    """POST a la API. Devuelve (status, respuesta). Un 400 no es excepción:
    es VISEC diciendo qué está mal en los datos, y lo decide quien llama."""
    url = config.visec_api_urls["url"] + ruta
    for intento in (1, 2):
        cabeceras = {
            "Authorization": f"Bearer {token(renovar=intento == 2)}",
            "current-company": config.visec_api_cuit,
            "Content-Type": "application/json",
        }
        try:
            r = requests.post(url, json=cuerpo, headers=cabeceras,
                              timeout=config.visec_api_timeout)
        except requests.RequestException as error:
            raise ErrorVisecApi(f"No se pudo llamar a {ruta}: {error}") from error
        # Un 401 con un token que creíamos vigente: se pide uno nuevo una vez.
        if r.status_code == 401 and intento == 1:
            continue
        break
    try:
        datos = r.json()
    except ValueError:
        datos = r.text
    _guardar_respuesta(ruta, cuerpo, r.status_code, datos)
    if r.status_code in (200, 400):
        return r.status_code, datos
    if r.status_code in (401, 403):
        raise ErrorVisecApi(
            f"VISEC no autorizó {ruta} (HTTP {r.status_code}). Revisá que el CUIT "
            f"{config.visec_api_cuit} esté habilitado para estas credenciales: {_texto_error(datos)}")
    raise ErrorVisecApi(f"VISEC respondió HTTP {r.status_code} en {ruta}: {_texto_error(datos)}")


def _guardar_respuesta(ruta: str, cuerpo: dict, status: int, datos: Any) -> None:
    """Pedido y respuesta, que es lo único que queda si hay que reclamar algo.
    El token va en las cabeceras, no acá: no se guarda ningún secreto."""
    try:
        RESPUESTAS_DIR.mkdir(parents=True, exist_ok=True)
        marca = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        nombre = ruta.strip("/").replace("/", "_")
        (RESPUESTAS_DIR / f"{marca}_{nombre}.json").write_text(
            json.dumps({"ambiente": config.visec_api_ambiente, "ruta": ruta, "status": status,
                        "pedido": cuerpo, "respuesta": datos},
                       ensure_ascii=False, indent=1, default=str),
            encoding="utf-8")
    except OSError as error:
        log.warning("No se pudo guardar la respuesta de VISEC: %s", error)


def _texto_error(datos: Any) -> str:
    """El errorResponse de VISEC en una línea legible."""
    if not isinstance(datos, dict):
        return str(datos or "")[:500]
    error = datos.get("error") if isinstance(datos.get("error"), dict) else datos
    partes = []
    for clave in ("errorMessage", "details"):
        if error.get(clave):
            partes.append(str(error[clave]))
    for v in error.get("validationErrors") or []:
        if isinstance(v, dict):
            campo = v.get("propertyName") or ""
            mensaje = v.get("errorMessage") or v.get("details") or ""
            partes.append(f"{campo}: {mensaje}" if campo else mensaje)
    return " · ".join(p for p in partes if p) or json.dumps(datos, ensure_ascii=False)[:500]


# ---------------------------------------------------------------------------
# De la fila de Visec al JSON de la API
# ---------------------------------------------------------------------------

def _entero(valor: Any) -> int | None:
    texto = re.sub(r"[^\d-]", "", str(valor or ""))
    try:
        return int(texto) if texto not in ("", "-") else None
    except ValueError:
        return None


def _positivo(valor: Any) -> int | None:
    """Un RUCA, un producto o un peso en 0 es "no hay", no el número 0: ARCA da
    planta 0 cuando el origen es un productor, y VISEC rechaza un RUCA 0."""
    numero = _entero(valor)
    return numero if numero and numero > 0 else None


def _campania(valor: str) -> str:
    """ARCA da la cosecha como 2526; VISEC la pide como 25/26."""
    texto = (valor or "").strip()
    if re.fullmatch(r"\d{4}", texto):
        return f"{texto[:2]}/{texto[2:]}"
    return texto


def _sin_vacios(datos: dict) -> dict:
    """Lo opcional que no se tiene no se manda, en vez de mandar "" o null."""
    return {k: v for k, v in datos.items() if v not in ("", None, [])}


def propios_de(fila: dict) -> dict[str, Any]:
    """Lo que armar_fila espera de "los datos del sistema", desde la fila de Oracle."""
    def texto(clave):
        valor = fila.get(clave)
        return "" if valor is None else str(valor).strip()
    return {
        "fecha_movimiento": fila.get("fecha_movimiento"),
        "peso_ingreso_stock": texto("peso_ingreso_stock"),
        "kilos_estimados": texto("kilos_estimados"),
        "renspa": texto("renspa"),
    }


def armar(consulta: Consulta, propios: dict) -> tuple[dict, list[str], visec.Fila]:
    """(JSON de la CPE, lo que falta, fila de Visec). Si falta algo, no se manda."""
    fila = visec.armar_fila(consulta, propios)
    v = fila.valores
    cpe = consulta.cpe or {}

    # Las fechas se rearman: en la fila están con el formato de la planilla.
    momento = propios.get("fecha_movimiento") or visec._buscar(cpe, "cabecera.fechaInicioEstado") \
        or visec._buscar(cpe, "cabecera.fechaEmision")
    fecha_movimiento = visec._fecha(momento, "%Y-%m-%dT%H:%M:%S")
    fecha_cpe = visec._fecha(visec._buscar(cpe, "cabecera.fechaEmision"), "%Y-%m-%d")

    movimiento = _sin_vacios({
        "numeroRENSPA": v["Número RENSPA"],
        "numeroCTGAsignado": v["Número CTG Asignado"],
        # Lo que salió de la unidad productiva es lo que pesó la balanza, no
        # IPL_KILOS_ESTIMADOS: un estimado de 30.000 contra 22.235 reales haría
        # aparecer al campo despachando de más en el control de stock de la UP.
        # (El XLSX sigue usando el estimado; este módulo no lo toca.)
        "pesoNetoCargaKgPorUP": _positivo(v["Peso Ingreso Stock (Kg)"]),
        "pesoNetoDescargaKgPorUP": _positivo(v["Peso Neto Descarga (Kg) por UP"]),
        "pesoIngresoStockKg": _positivo(v["Peso Ingreso Stock (Kg)"]),
        "ultimoAlmacenamiento": v["Último Almacenamiento"],
        "tipoMovimiento": config.visec_api_tipo_movimiento,
    })
    carta = _sin_vacios({
        "fechaHoraMovimiento": f"{fecha_movimiento}{ZONA}" if fecha_movimiento else "",
        "fechaCPE": fecha_cpe,
        "numeroCPE": v["Número CPE"],
        "numeroCTG": v["Número CTG"],
        "cuitTitular": v["CUIT Titular"],
        "numeroRUCAOrigen": _positivo(v["Número RUCA Origen"]),
        "cuitRemitenteComercialProductor": v["CUIT Remitente Comercial Productor"],
        "cuitRemitenteComercialVentaPrimaria": v["CUIT Rte Comercial Venta Primaria"],
        "cuitRemitenteComercialVentaSecundaria": v["CUIT Rte Comercial Venta Secundaria"],
        "cuitRemitenteComercialVentaSecundaria2": v["CUIT Rte Comercial Venta Secundaria 2"],
        "cuitCorredorVentaPrimaria": v["CUIT Corredor Venta Primaria"],
        "cuitCorredorVentaSecundaria": v["CUIT Corredor Venta Secundaria"],
        "cuitDestinatario": v["CUIT Destinatario"],
        "cuitDestino": v["CUIT Destino"],
        "numeroRUCADestino": _positivo(v["Número RUCA Destino"]),
        "producto": _positivo(v["Código Producto"]),
        "campania": _campania(v["Campaña"]),
        "pesoNetoCargaKg": v["Peso Neto Carga (Kg)"],
    })
    carta["movimientoMercaderiaUP"] = [movimiento]

    # Lo que la API marca como obligatorio, más fecha y peso: un movimiento sin
    # cuándo ni cuánto no tiene sentido mandarlo aunque el servicio lo acepte.
    obligatorios = {
        "numeroCTG": "el CTG", "cuitDestinatario": "el CUIT del destinatario",
        "cuitDestino": "el CUIT del destino", "numeroRUCADestino": "el RUCA de destino",
        "producto": "el código de producto", "campania": "la campaña",
        "fechaHoraMovimiento": "la fecha del movimiento",
    }
    faltan = [f"Falta {texto}" for campo, texto in obligatorios.items() if not carta.get(campo)]
    if not movimiento.get("pesoIngresoStockKg"):
        faltan.append("Falta el peso que ingresó a stock")
    # El esquema no lo marca obligatorio, pero la descripción de
    # movimientoMercaderiaUP sí: si la CPE viene de un productor (sin RUCA de
    # origen), cada movimiento es una unidad productiva y va con su RENSPA. Sin
    # él no hay trazabilidad, que es para lo que existe Visec. Queda en "datos
    # faltantes", que se reintenta solo: sale en cuanto se cargue en el ERP.
    if not carta.get("numeroRUCAOrigen") and not movimiento.get("numeroRENSPA"):
        faltan.append("Falta el RENSPA: viene de un productor y Visec pide la unidad productiva")
    return carta, faltan, fila


# ---------------------------------------------------------------------------
# Estado de cada CTG
# ---------------------------------------------------------------------------

_candado_estado = threading.Lock()


def estado() -> dict[str, dict]:
    if not ESTADO.is_file():
        return {}
    try:
        return json.loads(ESTADO.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        log.warning("No se pudo leer el estado de VISEC: %s", error)
        return {}


def _guardar_estado(datos: dict) -> None:
    VISEC_API_DIR.mkdir(parents=True, exist_ok=True)
    temporal = ESTADO.with_suffix(".tmp")
    temporal.write_text(json.dumps(datos, ensure_ascii=False, indent=1, sort_keys=True),
                        encoding="utf-8")
    temporal.replace(ESTADO)


def anotar(cambios: dict[str, dict]) -> None:
    """Aplica varios cambios de una sola escritura: {ctg: {estado, ...}}."""
    with _candado_estado:
        datos = estado()
        ahora = datetime.now().strftime(FORMATO)
        for ctg, cambio in cambios.items():
            anterior = datos.get(ctg, {})
            nuevo = {**anterior, **cambio, "cuando": ahora,
                     "intentos": int(anterior.get("intentos", 0)) + (1 if cambio.get("enviando") else 0)}
            nuevo.pop("enviando", None)
            datos[ctg] = nuevo
        _guardar_estado(datos)


def olvidar(ctgs: list[str]) -> int:
    """Para volver a mandar una rechazada, una vez corregida del otro lado."""
    with _candado_estado:
        datos = estado()
        cuantos = 0
        for ctg in ctgs:
            if datos.pop(ctg, None) is not None:
                cuantos += 1
        if cuantos:
            _guardar_estado(datos)
    return cuantos


def resumen_estado() -> dict[str, int]:
    conteo: dict[str, int] = {}
    for d in estado().values():
        conteo[d.get("estado", "")] = conteo.get(d.get("estado", ""), 0) + 1
    return conteo


# ---------------------------------------------------------------------------
# Envío y seguimiento
# ---------------------------------------------------------------------------

def enviar(cpes: list[tuple[str, dict]]) -> dict[str, dict]:
    """Manda un lote. Si VISEC rechaza el lote entero (400 de prevalidación),
    se manda de a una para que una CPE mal no frene a las demás."""
    if not cpes:
        return {}
    status, datos = _llamar(RUTA_ALTA, {"cuitEmpresa": config.visec_api_cuit,
                                        "cartaPorte": [cuerpo for _, cuerpo in cpes]})
    if status == 200 and isinstance(datos, dict) and datos.get("numero"):
        numero = int(datos["numero"])
        log.info("VISEC: proceso %s con %s CPE", numero, len(cpes))
        return {ctg: {"estado": ENVIADA, "proceso": numero, "detalle": "", "enviando": True}
                for ctg, _ in cpes}
    if status == 200:
        motivo = f"VISEC respondió 200 sin número de proceso: {_texto_error(datos)}"
        return {ctg: {"estado": ERROR, "detalle": motivo} for ctg, _ in cpes}
    if len(cpes) > 1:
        log.info("VISEC rechazó un lote de %s; se manda de a una", len(cpes))
        resultado: dict[str, dict] = {}
        for una in cpes:
            resultado.update(enviar([una]))
        return resultado
    ctg = cpes[0][0]
    return {ctg: {"estado": RECHAZADA, "detalle": _texto_error(datos), "enviando": True}}


def consultar_proceso(numero: int) -> tuple[int, Any]:
    return _llamar(RUTA_PROCESO, {"cuitEmpresaResponsable": config.visec_api_cuit,
                                  "numeroProceso": int(numero)})


def revisar_procesos() -> dict[str, int]:
    """Pregunta el resultado de los procesos que quedaron esperando."""
    pendientes: dict[int, list[str]] = {}
    for ctg, d in estado().items():
        if d.get("estado") == ENVIADA and d.get("proceso"):
            pendientes.setdefault(int(d["proceso"]), []).append(ctg)
    conteo = {REGISTRADA: 0, RECHAZADA: 0, ENVIADA: 0}
    cambios: dict[str, dict] = {}
    for numero, ctgs in pendientes.items():
        try:
            status, datos = consultar_proceso(numero)
        except ErrorVisecApi as error:
            log.warning("No se pudo consultar el proceso %s: %s", numero, error)
            conteo[ENVIADA] += len(ctgs)
            continue
        if status != 200 or not isinstance(datos, dict):
            log.warning("Proceso %s: HTTP %s %s", numero, status, _texto_error(datos))
            conteo[ENVIADA] += len(ctgs)
            continue
        detalles = {str(d.get("numeroCTG") or "").strip(): d
                    for d in datos.get("detalles") or [] if isinstance(d, dict)}
        procesados = int(datos.get("documentosProcesados") or 0)
        terminado = procesados >= len(ctgs)
        descripcion_proceso = str(datos.get("descripcionEstado") or "")
        for ctg in ctgs:
            detalle = detalles.get(ctg)
            if detalle is None:
                conteo[ENVIADA] += 1
                if terminado:
                    cambios[ctg] = {"estado": ENVIADA,
                                    "detalle": f"El proceso {numero} terminó ({descripcion_proceso}) "
                                               "pero no trae resultado para este CTG"}
                continue
            errores = [f"{e.get('codigoError') or ''} {e.get('observaciones') or ''}".strip()
                       for e in detalle.get("errores") or [] if isinstance(e, dict)]
            descripcion = str((detalle.get("estado") or {}).get("descripcion") or "")
            if errores or PALABRAS_DE_RECHAZO.search(descripcion):
                cambios[ctg] = {"estado": RECHAZADA,
                                "detalle": " · ".join(errores) or descripcion,
                                "estado_visec": descripcion}
                conteo[RECHAZADA] += 1
            elif terminado:
                cambios[ctg] = {"estado": REGISTRADA, "detalle": "", "estado_visec": descripcion}
                conteo[REGISTRADA] += 1
            else:
                cambios[ctg] = {"estado": ENVIADA, "estado_visec": descripcion,
                                "detalle": f"Procesando: {descripcion_proceso}"}
                conteo[ENVIADA] += 1
    if cambios:
        anotar(cambios)
    return conteo


# ---------------------------------------------------------------------------
# La corrida
# ---------------------------------------------------------------------------

_candado_corrida = threading.Lock()


def periodo(desde: datetime | None = None, hasta: datetime | None = None) -> tuple[datetime, datetime, str]:
    """Qué período mira una corrida. Sin fechas: los últimos N días, nunca antes
    de la fecha inicial."""
    hasta = (hasta or datetime.now()).replace(microsecond=0)
    if desde is not None:
        return desde, hasta, "las fechas elegidas"
    desde = hasta - timedelta(days=max(config.visec_api_ventana_dias, 1))
    origen = f"los últimos {config.visec_api_ventana_dias} días"
    if config.visec_api_fecha_inicial:
        try:
            inicial = datetime.strptime(config.visec_api_fecha_inicial, "%Y-%m-%d")
        except ValueError as error:
            raise ErrorVisecApi("VISEC_API_FECHA_INICIAL tiene que ser AAAA-MM-DD") from error
        if inicial > desde:
            desde, origen = inicial, "VISEC_API_FECHA_INICIAL"
    return desde, hasta, origen


def corrida(enviar_de_verdad: bool = True,
            desde: datetime | None = None,
            hasta: datetime | None = None,
            ctgs: list[str] | None = None) -> dict:
    """Busca en Oracle, completa con ARCA y manda a VISEC lo que esté listo.

    Una sola a la vez: la automática corre cada pocos minutos y alguien puede
    apretar el botón en el medio, y dos corridas juntas mandarían dos veces.
    """
    if not _candado_corrida.acquire(blocking=False):
        raise ErrorVisecApi("Ya hay una corrida de VISEC en curso. Probá en un rato.")
    try:
        return _corrida(enviar_de_verdad, desde, hasta, ctgs)
    finally:
        _candado_corrida.release()


def _corrida(enviar_de_verdad, desde, hasta, ctgs) -> dict:
    from . import oracle, wscpe

    resumen: dict[str, Any] = {"enviar": enviar_de_verdad, "ambiente": config.visec_api_ambiente,
                               "procesos": {}, "filas": [], "por_estado": {},
                               "incompleta": False, "encontradas": 0}
    if enviar_de_verdad:
        resumen["procesos"] = revisar_procesos()

    if ctgs:
        filas = oracle.buscar_visec(ctgs=ctgs)
        resumen.update(desde=None, hasta=None, origen="los CTG pedidos")
    else:
        d, h, origen = periodo(desde, hasta)
        filas = oracle.buscar_visec(desde=d, hasta=h, especies=config.visec_api_especies)
        resumen.update(desde=d, hasta=h, origen=origen)

    # El mismo CTG puede estar en dos esquemas; va una sola vez.
    unicas: dict[str, dict] = {}
    for fila in filas:
        unicas.setdefault(fila["ctg"], fila)
    resumen["encontradas"] = len(unicas)

    ya = estado()
    candidatas = [f for ctg, f in unicas.items()
                  if ya.get(ctg, {}).get("estado") not in NO_REINTENTAR]
    limite = max(config.visec_api_limite, 1)
    resumen["incompleta"] = len(candidatas) > limite
    resumen["ya_resueltas"] = len(unicas) - len(candidatas)

    listas: list[tuple[str, dict]] = []
    cambios: dict[str, dict] = {}
    for fila in candidatas[:limite]:
        ctg = fila["ctg"]
        base = {"ctg": ctg, "sociedad": fila.get("sociedad", ""),
                "fecha": fila.get("fecha_movimiento") or fila.get("fecha_hora")}
        try:
            consulta = wscpe.consultar(ctg)
        except Exception as error:  # ARCA caído no frena la corrida
            log.exception("ARCA, CTG %s", ctg)
            consulta = Consulta(ctg, errores=[str(error)])
        if not consulta.ok:
            motivo = "ARCA no la devolvió: " + "; ".join(consulta.errores or ["sin detalle"])
            cambios[ctg] = {"estado": ERROR, "detalle": motivo}
            resumen["filas"].append({**base, "estado": ERROR, "detalle": motivo})
            continue
        codigo = consulta.estado.upper()
        if codigo not in visec.ESTADOS_FINALES:
            motivo = f"ARCA la tiene en {visec.ESTADOS.get(codigo, codigo) or 'estado desconocido'}"
            cambios[ctg] = {"estado": ESPERANDO_ARCA, "detalle": motivo, "estado_arca": codigo}
            resumen["filas"].append({**base, "estado": ESPERANDO_ARCA, "detalle": motivo})
            continue
        cuerpo, faltan, _ = armar(consulta, propios_de(fila))
        if faltan:
            motivo = "; ".join(faltan)
            cambios[ctg] = {"estado": FALTAN_DATOS, "detalle": motivo, "estado_arca": codigo}
            resumen["filas"].append({**base, "estado": FALTAN_DATOS, "detalle": motivo})
            continue
        listas.append((ctg, cuerpo))
        resumen["filas"].append({**base, "estado": "lista", "detalle": "",
                                 "ruca_destino": cuerpo.get("numeroRUCADestino"),
                                 "renspa": cuerpo["movimientoMercaderiaUP"][0].get("numeroRENSPA", ""),
                                 "kilos": cuerpo["movimientoMercaderiaUP"][0].get("pesoIngresoStockKg"),
                                 "json": cuerpo})

    if enviar_de_verdad:
        lote = max(config.visec_api_lote, 1)
        for inicio in range(0, len(listas), lote):
            try:
                cambios.update(enviar(listas[inicio:inicio + lote]))
            except ErrorVisecApi as error:
                log.warning("VISEC: %s", error)
                for ctg, _ in listas[inicio:inicio + lote]:
                    cambios[ctg] = {"estado": ERROR, "detalle": str(error)}
        if cambios:
            anotar(cambios)
        for f in resumen["filas"]:
            if f["ctg"] in cambios:
                f["estado"] = cambios[f["ctg"]]["estado"]
                f["detalle"] = cambios[f["ctg"]].get("detalle", "")
                f["proceso"] = cambios[f["ctg"]].get("proceso")

    for f in resumen["filas"]:
        resumen["por_estado"][f["estado"]] = resumen["por_estado"].get(f["estado"], 0) + 1
    return resumen


# ---------------------------------------------------------------------------
# La corrida automática, para la pantalla
# ---------------------------------------------------------------------------

def anotar_automatica(resumen: dict | None = None, error: str = "") -> None:
    datos: dict[str, Any] = {"cuando": datetime.now().strftime(FORMATO), "error": error}
    if resumen:
        datos.update(encontradas=resumen.get("encontradas", 0),
                     por_estado=resumen.get("por_estado", {}),
                     procesos=resumen.get("procesos", {}))
    try:
        VISEC_API_DIR.mkdir(parents=True, exist_ok=True)
        temporal = ULTIMA_AUTOMATICA.with_suffix(".tmp")
        temporal.write_text(json.dumps(datos, ensure_ascii=False), encoding="utf-8")
        temporal.replace(ULTIMA_AUTOMATICA)
    except OSError as error_io:
        log.warning("No se pudo anotar la corrida automática de VISEC: %s", error_io)


def ultima_automatica() -> dict | None:
    if not ULTIMA_AUTOMATICA.is_file():
        return None
    try:
        datos = json.loads(ULTIMA_AUTOMATICA.read_text(encoding="utf-8"))
        datos["momento"] = datetime.strptime(datos["cuando"], FORMATO)
        return datos
    except (OSError, ValueError, KeyError):
        return None
