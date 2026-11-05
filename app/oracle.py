"""Lectura de los ingresos a planta del ERP (Oracle), por número de CTG.

Reemplaza la exportación manual de SoftCereal: en vez de pedirle al operario
que corra una consulta y baje un Excel, la app consulta la base directamente.

La base es Oracle 11.2, así que hace falta el modo "thick" de python-oracledb
(el modo "thin", que no necesita nada instalado, pide 12.1 o posterior). El
Instant Client va en la imagen; ver el Dockerfile.

Las tres sociedades (Rivara, La Tranquera Verde, Pradera Natural) son esquemas
distintos de la misma base, así que una sola conexión las lee a todas. Como el
esquema no se puede pasar por parámetro, el nombre se valida contra la lista
del .env antes de interpolarlo.
"""

from __future__ import annotations

import logging
import re
import threading

from .config import config

log = logging.getLogger(__name__)

_inicializado = False
_candado = threading.Lock()

# Un CTG de ARCA son 11 dígitos. Se valida antes de consultar.
CTG = re.compile(r"\d{11}")

# Los campos que necesita la planilla de Albor. El resto de las columnas de
# INGRESOS_PLANTAS no se traen: son 200 y nos interesan 40.
CONSULTA = """
SELECT i.IPL_CTG                        AS ctg,
       i.IPL_PUNTO_INGRESO              AS punto_ingreso,
       i.IPL_NUMERO_INGRESO             AS numero_ingreso,
       i.IPL_SUCURSAL_TICKET            AS sucursal_ticket,
       i.IPL_NUMERO_TICKET              AS numero_ticket,
       i.IPL_FECHA_HORA_CARGA           AS fecha_carga,
       i.IPL_FECHA_HORA                 AS fecha_hora,
       i.IPL_COSECHA                    AS cosecha,
       i.IPL_ESPECIE                    AS especie_codigo,
       e.ESP_DESCRIPCION                AS especie,
       i.IPL_CAMPO                      AS campo_codigo,
       c.CAM_DESCRIPCION                AS campo,
       i.IPL_LOTE                       AS lote,
       i.IPL_PLANTA                     AS planta_codigo,
       p.PLA_DESCRIPCION                AS planta,
       i.IPL_NOMBRE_PRODUCTOR           AS productor,
       i.IPL_CUIT_PRODUCTOR             AS cuit_productor,
       i.IPL_NOMBRE_EMPRESA_TRANSPORTE  AS transportista,
       i.IPL_CUIT_TRANSPORTE            AS cuit_transportista,
       i.IPL_NOMBRE_CAMIONERO           AS chofer,
       ch.ENT_CUIT                      AS cuit_chofer,
       i.IPL_PATENTE_CAMION             AS patente_camion,
       i.IPL_PATENTE_ACOPLADO           AS patente_acoplado,
       i.IPL_PESO_BRUTO_RECONOCIDO      AS bruto,
       i.IPL_TARA                       AS tara,
       i.IPL_PESO_BRUTO_MERCADERIA      AS neto,
       i.IPL_HUMEDAD                    AS humedad,
       i.IPL_PESO_NETO                  AS peso_neto,
       i.IPL_KILOS_ESTIMADOS            AS kilos_estimados,
       i.IPL_SUCURSAL_INTERNA_CPE       AS sucursal_cpe,
       i.IPL_NUMERO_INTERNO_CPE         AS numero_cpe,
       i.IPL_CPE_ELECTRONICA            AS cpe_electronica,
       i.IPL_CTG_FLETE_CORTO            AS flete_corto,
       i.IPL_NUMERO_RENSPA              AS renspa,
       i.IPL_KILOMETROS_ACARREO         AS kilometros,
       i.IPL_OBSERVACIONES_CARGA        AS observaciones
  FROM {esquema}.INGRESOS_PLANTAS i
  LEFT JOIN {esquema}.ESPECIES  e ON e.ESP_ESPECIE = i.IPL_ESPECIE
  LEFT JOIN {esquema}.CAMPOS    c ON c.CAM_CAMPO   = i.IPL_CAMPO
  LEFT JOIN {esquema}.PLANTAS   p ON p.PLA_PLANTA  = i.IPL_PLANTA
  -- El CUIT del chofer no está en INGRESOS_PLANTAS, solo su código y su DNI.
  LEFT JOIN {esquema}.ENTIDADES ch ON TRIM(ch.ENT_CODIGO) = TRIM(i.IPL_CAMIONERO)
 WHERE i.IPL_CTG IN ({marcas})
"""


# Lo que necesita el alta en BolsaTech. Tiene más vueltas que la de Albor: el
# establecimiento y la localidad salen del campo, y el id de muestra de la
# tabla de biotecnologías, que se cruza por CTG.
CONSULTA_BOLSATECH = """
SELECT i.IPL_PUNTO_INGRESO              AS punto_ingreso,
       i.IPL_NUMERO_INGRESO             AS numero_ingreso,
       i.IPL_SUCURSAL_INTERNA_CPE       AS sucursal_cpe,
       i.IPL_NUMERO_INTERNO_CPE         AS numero_cpe,
       i.IPL_CTG                        AS ctg,
       i.IPL_ESPECIE                    AS especie_codigo,
       e.ESP_DESCRIPCION                AS especie,
       e.ESP_CODIGO_SAGPYA              AS especie_sagpya,
       i.IPL_CUIT_PRODUCTOR             AS cuit_productor,
       i.IPL_NOMBRE_PRODUCTOR           AS productor,
       i.IPL_CAMPO                      AS campo_codigo,
       c.CAM_DESCRIPCION                AS establecimiento,
       cp.CPO_LOCALIDAD_ONCCA           AS localidad_procedencia,
       i.IPL_PLANTA                     AS planta_codigo,
       p.PLA_DESCRIPCION                AS planta,
       p.PLA_NUMERO_ONCCA               AS planta_oncca,
       i.IPL_PESO_NETO                  AS kilos_netos_secos,
       i.IPL_FECHA_HORA                 AS fecha_hora,
       i.IPL_COSECHA                    AS cosecha,
       (SELECT MAX(TRIM(TO_CHAR(b.INB_IDENTIFICADOR_MUESTRA)))
          FROM {esquema}.INFORMACION_BIOTECNOLOGIAS b
         WHERE b.INB_NUMERO_CP = i.IPL_CTG
           AND b.INB_IDENTIFICADOR_MUESTRA IS NOT NULL) AS id_muestra
  FROM {esquema}.INGRESOS_PLANTAS i
  LEFT JOIN {esquema}.ESPECIES e ON e.ESP_ESPECIE = i.IPL_ESPECIE
  LEFT JOIN {esquema}.CAMPOS   c ON c.CAM_CAMPO   = i.IPL_CAMPO
  LEFT JOIN {esquema}.CODIGOS_POSTALES cp
         ON cp.CPO_CODIGO_POSTAL = c.CAM_CODIGO_POSTAL
        AND NVL(TRIM(cp.CPO_SUFIJO_POSTAL), ' ') = NVL(TRIM(c.CAM_SUFIJO_POSTAL), ' ')
  LEFT JOIN {esquema}.PLANTAS  p ON p.PLA_PLANTA  = i.IPL_PLANTA
 WHERE i.IPL_NUMERO_INTERNO_CPE IS NOT NULL
   AND i.IPL_CTG IS NOT NULL
   AND i.IPL_PESO_NETO IS NOT NULL
   AND i.IPL_PESO_NETO > 0
   AND {filtro}
 ORDER BY i.IPL_FECHA_HORA, i.IPL_PUNTO_INGRESO, i.IPL_NUMERO_INGRESO
"""


# Para VISEC por API. Son las columnas que ya saca scripts/ctg_soja.py para el
# XLSX: lo que ARCA no tiene. El resto de la CPE sale de ARCA, como en el XLSX.
CONSULTA_VISEC = """
SELECT i.IPL_CTG                                         AS ctg,
       NVL(i.IPL_FECHA_HORA_CONF_ARRIBO, i.IPL_FECHA_HORA) AS fecha_movimiento,
       i.IPL_PESO_NETO                                   AS peso_ingreso_stock,
       i.IPL_KILOS_ESTIMADOS                             AS kilos_estimados,
       TRIM(i.IPL_NUMERO_RENSPA)                         AS renspa,
       i.IPL_PUNTO_INGRESO                               AS punto_ingreso,
       i.IPL_NUMERO_INGRESO                              AS numero_ingreso,
       i.IPL_PLANTA                                      AS planta_codigo,
       i.IPL_ESPECIE                                     AS especie_codigo,
       i.IPL_COSECHA                                     AS cosecha,
       i.IPL_FECHA_HORA                                  AS fecha_hora
  FROM {esquema}.INGRESOS_PLANTAS i
 WHERE i.IPL_CTG IS NOT NULL
   AND i.IPL_PESO_NETO IS NOT NULL
   AND i.IPL_PESO_NETO > 0
   AND {filtro}
 ORDER BY i.IPL_FECHA_HORA, i.IPL_PUNTO_INGRESO, i.IPL_NUMERO_INGRESO
"""


# Egresos para VISEC. Visec resuelve sola la traza (de qué entradas sale cada
# camión), así que alcanza con el CTG, la fecha de carga y el peso; el resto
# sale de ARCA. EPL_FECHA_DEL_MOVIMIENTO viene vacía en la práctica, por eso
# manda EPL_FECHA_HORA_BRUTO: es cuando el camión ya está cargado.
# EPL_TIPO_EGRESO: P retiro de productor, C contrato de venta.
CONSULTA_VISEC_EGRESOS = """
SELECT TRIM(e.EPL_CTG)                                       AS ctg,
       NVL(e.EPL_FECHA_HORA_BRUTO, e.EPL_FECHA_DEL_MOVIMIENTO) AS fecha_movimiento,
       e.EPL_PESO_MERCADERIA                                 AS peso_carga,
       e.EPL_TIPO_EGRESO                                     AS tipo_egreso,
       TRIM(e.EPL_CUIT_PRODUCTOR)                            AS cuit_productor,
       e.EPL_PUNTO_EGRESO                                    AS punto_egreso,
       e.EPL_NUMERO_EGRESO                                   AS numero_egreso,
       e.EPL_PLANTA                                          AS planta_codigo,
       e.EPL_ESPECIE                                         AS especie_codigo,
       e.EPL_COSECHA                                         AS cosecha,
       e.EPL_FECHA_HORA_BRUTO                                AS fecha_hora
  FROM {esquema}.EGRESOS_PLANTAS e
 WHERE e.EPL_CTG IS NOT NULL
   AND e.EPL_PESO_MERCADERIA IS NOT NULL
   AND e.EPL_PESO_MERCADERIA > 0
   AND {filtro}
 ORDER BY e.EPL_FECHA_HORA_BRUTO, e.EPL_PUNTO_EGRESO, e.EPL_NUMERO_EGRESO
"""

# Las columnas por las que filtran _filtros, en cada tabla.
COLUMNAS_INGRESOS = {"ctg": "i.IPL_CTG", "fecha": "i.IPL_FECHA_HORA", "especie": "i.IPL_ESPECIE"}
COLUMNAS_EGRESOS = {"ctg": "TRIM(e.EPL_CTG)", "fecha": "e.EPL_FECHA_HORA_BRUTO",
                    "especie": "TRIM(e.EPL_ESPECIE)"}


class ErrorOracle(RuntimeError):
    pass


def _inicializar() -> None:
    """Carga el Instant Client. Una sola vez por proceso: oracledb falla si se
    llama dos veces."""
    global _inicializado
    with _candado:
        if _inicializado:
            return
        import oracledb

        try:
            if config.oracle_lib_dir:
                oracledb.init_oracle_client(lib_dir=config.oracle_lib_dir)
            else:
                # En Linux las librerías se encuentran por ldconfig, no por
                # lib_dir: el Dockerfile las registra en /etc/ld.so.conf.d.
                oracledb.init_oracle_client()
        except Exception as error:  # ya inicializado, o falta el Instant Client
            if "DPI-1047" in str(error):
                raise ErrorOracle(
                    "No se encuentra el Oracle Instant Client en el contenedor. "
                    f"Revisá el Dockerfile y ORACLE_LIB_DIR. Detalle: {error}"
                ) from error
            log.info("init_oracle_client: %s", error)
        _inicializado = True


def conectar():
    import oracledb

    problemas = config.problemas_oracle()
    if problemas:
        raise ErrorOracle(" · ".join(problemas))
    _inicializar()
    try:
        return oracledb.connect(
            user=config.oracle_usuario,
            password=config.oracle_clave,
            dsn=config.oracle_dsn,
        )
    except Exception as error:
        raise ErrorOracle(f"No se pudo conectar a Oracle ({config.oracle_dsn}): {error}") from error


def probar() -> str:
    """Conecta y devuelve la versión del servidor. Para el botón de prueba."""
    with conectar() as cn:
        return cn.version


def _esquemas_validos(esquemas: list[str] | None) -> list[str]:
    """Los esquemas configurados. El nombre se interpola en el SQL, así que
    nunca puede venir de la pantalla: solo del .env."""
    configurados = list(config.oracle_esquemas)
    if esquemas is None:
        return configurados
    elegidos = [e for e in esquemas if e in configurados]
    if not elegidos:
        raise ErrorOracle("Ningún esquema válido. Revisá ORACLE_ESQUEMAS en el .env.")
    return elegidos


def buscar(ctgs: list[str], esquemas: list[str] | None = None) -> list[dict]:
    """Los ingresos de esos CTG, en todos los esquemas configurados.

    Un CTG existe en un solo esquema, pero cuál no se sabe de antemano, así que
    se busca en todos. Cada fila sale con la sociedad en la que apareció.
    """
    numeros = [c for c in dict.fromkeys(ctgs) if CTG.fullmatch(c)]
    if not numeros:
        return []
    filas: list[dict] = []
    with conectar() as cn:
        cur = cn.cursor()
        for esquema in _esquemas_validos(esquemas):
            for tanda in _por_tandas(numeros, 500):
                # Bind variables, no interpolación: el CTG viene de la pantalla.
                marcas = ", ".join(f":c{i}" for i in range(len(tanda)))
                cur.execute(
                    CONSULTA.format(esquema=esquema, marcas=marcas),
                    {f"c{i}": v for i, v in enumerate(tanda)},
                )
                columnas = [d[0].lower() for d in cur.description]
                for fila in cur:
                    datos = dict(zip(columnas, fila))
                    datos["esquema"] = esquema
                    datos["sociedad"] = config.oracle_esquemas[esquema]
                    filas.append(datos)
    log.info("Oracle: %s CTG pedidos, %s encontrados", len(numeros), len(filas))
    return filas


def buscar_bolsatech(
    ctgs: list[str] | None = None,
    desde=None,
    hasta=None,
    especies: list[str] | None = None,
    esquemas: list[str] | None = None,
) -> list[dict]:
    """Los ingresos a informar a BolsaTech, por CTG o por rango de fechas.

    Por CTG no se filtra la especie: si el usuario pegó ese número es porque lo
    quiere informar. Por fecha sí, que es como corre el proceso automático.
    """
    filas: list[dict] = []
    numeros = [c for c in dict.fromkeys(ctgs or []) if CTG.fullmatch(c)]
    if ctgs is not None and not numeros:
        return []
    with conectar() as cn:
        cur = cn.cursor()
        for esquema in _esquemas_validos(esquemas):
            for filtro, parametros in _filtros(numeros, desde, hasta, especies):
                cur.execute(CONSULTA_BOLSATECH.format(esquema=esquema, filtro=filtro), parametros)
                columnas = [d[0].lower() for d in cur.description]
                for fila in cur:
                    datos = dict(zip(columnas, fila))
                    datos["esquema"] = esquema
                    datos["sociedad"] = config.oracle_esquemas[esquema]
                    filas.append(datos)
    log.info("Oracle (BolsaTech): %s ingresos", len(filas))
    return filas


def buscar_visec(
    ctgs: list[str] | None = None,
    desde=None,
    hasta=None,
    especies: list[str] | None = None,
    esquemas: list[str] | None = None,
    egresos: bool = False,
) -> list[dict]:
    """Los ingresos (o los egresos) a informar a VISEC, por CTG o por fechas.

    Igual que en BolsaTech: por CTG no se filtra la especie (si alguien lo pegó
    es porque lo quiere informar); por fecha sí. Cada fila lleva "tipo": 1 si
    es un ingreso y 2 si es un egreso, que es el tipoMovimiento de Visec.
    """
    consulta = CONSULTA_VISEC_EGRESOS if egresos else CONSULTA_VISEC
    columnas_filtro = COLUMNAS_EGRESOS if egresos else COLUMNAS_INGRESOS
    filas: list[dict] = []
    numeros = [c for c in dict.fromkeys(ctgs or []) if CTG.fullmatch(c)]
    if ctgs is not None and not numeros:
        return []
    with conectar() as cn:
        cur = cn.cursor()
        for esquema in _esquemas_validos(esquemas):
            for filtro, parametros in _filtros(numeros, desde, hasta, especies, columnas_filtro):
                cur.execute(consulta.format(esquema=esquema, filtro=filtro), parametros)
                columnas = [d[0].lower() for d in cur.description]
                for fila in cur:
                    datos = dict(zip(columnas, fila))
                    datos["ctg"] = str(datos.get("ctg") or "").strip()
                    datos["tipo"] = 2 if egresos else 1
                    datos["esquema"] = esquema
                    datos["sociedad"] = config.oracle_esquemas[esquema]
                    filas.append(datos)
    log.info("Oracle (VISEC): %s %s", len(filas), "egresos" if egresos else "ingresos")
    return filas


def _filtros(numeros: list[str], desde, hasta, especies: list[str] | None,
             columnas: dict[str, str] | None = None):
    """(trozo de WHERE, parámetros). Todo va por bind variables.

    `columnas` son nombres fijos del código (COLUMNAS_INGRESOS o _EGRESOS),
    nunca algo que venga de la pantalla."""
    col = columnas or COLUMNAS_INGRESOS
    if numeros:
        for tanda in _por_tandas(numeros, 500):
            marcas = ", ".join(f":c{i}" for i in range(len(tanda)))
            yield (f"{col['ctg']} IN ({marcas})",
                   {f"c{i}": v for i, v in enumerate(tanda)})
        return
    if not (desde and hasta):
        raise ErrorOracle("Hace falta una lista de CTG o un rango de fechas.")
    condiciones = [f"{col['fecha']} > :desde", f"{col['fecha']} <= :hasta"]
    parametros: dict = {"desde": desde, "hasta": hasta}
    if especies:
        marcas = ", ".join(f":e{i}" for i in range(len(especies)))
        condiciones.append(f"{col['especie']} IN ({marcas})")
        parametros.update({f"e{i}": v for i, v in enumerate(especies)})
    yield " AND ".join(condiciones), parametros


def _por_tandas(valores: list[str], tamano: int):
    """Oracle no acepta más de 1000 elementos en un IN."""
    for inicio in range(0, len(valores), tamano):
        yield valores[inicio:inicio + tamano]
