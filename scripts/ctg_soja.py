#!/usr/bin/env python3
"""Lista los CTG de los ingresos de soja (especie 38) para informar a Visec.

Pensado para copiarse a una carpeta cualquiera del servidor que tiene SQL*Plus
(por ejemplo el Escritorio) y correrse ahi: lee el .env y escribe el CSV en la
misma carpeta donde esta este archivo.

No necesita instalar nada: usa solo la biblioteca estandar de Python 3.8+.

Consulta INGRESOS_PLANTAS de un esquema por sociedad (SYSADMIN de Rivara,
SYSADMIN_ELN de La Tranquera Verde, SYSADMIN_PRA de Pradera Natural). Por
omision consulta los tres y agrega al CSV de que empresa es cada CTG.

Ademas del CTG exporta lo que ARCA no tiene: la fecha del movimiento, el peso
neto de balanza, los kilos estimados y el RENSPA. El CSV resultante se sube a la app
"Informar CPE a Visec".

Uso:
  python ctg_soja.py                          (los ingresos de hoy, los 3 esquemas)
  python ctg_soja.py --desde 2026-10-01
  python ctg_soja.py --desde 2026-10-01 --hasta 2026-10-07
  python ctg_soja.py --desde 2026-10-01 --esquema SYSADMIN_PRA
  python ctg_soja.py --desde 2026-10-01 --planta 2
"""

from __future__ import annotations

import argparse
import csv
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

# La carpeta donde esta este archivo: de aca sale el .env y aca va el CSV.
BASE_DIR = Path(__file__).resolve().parent
ARCHIVO_ENV = BASE_DIR / ".env"

ESPECIE_SOJA = "38"  # codigo interno del sistema; es el grano que pide Visec

PLANTILLA_ENV = r"""# Credenciales de Oracle: las mismas que usa cpe_bolsatech.py.
# Este archivo tiene la clave de la base: no lo compartas ni lo subas a ningun
# repositorio.

SQLPLUS_EXE=D:\oracle\product\11.2.0\client_1\bin\sqlplus.exe
ORACLE_USUARIO=
ORACLE_PASSWORD=
ORACLE_ALIAS=BASE

# Un esquema por sociedad, en formato ESQUEMA:Nombre separados por coma.
# Las credenciales son las mismas para los tres. Por omision se consultan
# todos; con --esquema se elige uno.
ESQUEMAS=SYSADMIN:Rivara,SYSADMIN_ELN:La Tranquera Verde,SYSADMIN_PRA:Pradera Natural
"""

# Esquemas que se usan si el .env no trae ESQUEMAS.
ESQUEMAS_POR_OMISION = {
    "SYSADMIN": "Rivara",
    "SYSADMIN_ELN": "La Tranquera Verde",
    "SYSADMIN_PRA": "Pradera Natural",
}

# "empresa" y "esquema" los agrega Python, no la consulta.
CAMPOS_SQL = [
    "ctg", "numero_cpe", "fecha_movimiento", "peso_ingreso_stock",
    "kilos_estimados", "renspa",
    "punto_ingreso", "numero_ingreso", "planta", "cosecha", "cuit_productor",
]
COLUMNAS = CAMPOS_SQL + ["empresa", "esquema"]

# El orden de los campos tiene que coincidir con CAMPOS_SQL.
SELECT = """
    NVL(TRIM(TO_CHAR(I.IPL_CTG)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_SUCURSAL_INTERNA_CPE)), '') || LPAD(NVL(TRIM(TO_CHAR(I.IPL_NUMERO_INTERNO_CPE)), ''), 8, '0') || '|' ||
    TO_CHAR(NVL(I.IPL_FECHA_HORA_CONF_ARRIBO, I.IPL_FECHA_HORA), 'YYYY-MM-DD HH24:MI:SS') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_PESO_NETO, 'FM999999999999990', 'NLS_NUMERIC_CHARACTERS=''.,''')), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_KILOS_ESTIMADOS, 'FM999999999999990', 'NLS_NUMERIC_CHARACTERS=''.,''')), '') || '|' ||
    REPLACE(NVL(TRIM(TO_CHAR(I.IPL_NUMERO_RENSPA)), ''), '|', ' ') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_PUNTO_INGRESO)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_NUMERO_INGRESO)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_PLANTA)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_COSECHA)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_CUIT_PRODUCTOR)), '')
"""


# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------

def leer_env() -> dict[str, str]:
    """Lee el .env de esta carpeta. Si no existe, lo crea y corta."""
    if not ARCHIVO_ENV.exists():
        ARCHIVO_ENV.write_text(PLANTILLA_ENV, encoding="utf-8")
        sys.exit(
            f"Se creo el archivo de configuracion:\n  {ARCHIVO_ENV}\n\n"
            "Abrilo, completa ORACLE_USUARIO, ORACLE_PASSWORD y la ruta de\n"
            "SQLPLUS_EXE, guardalo y volve a ejecutar."
        )

    valores: dict[str, str] = {}
    for linea in ARCHIVO_ENV.read_text(encoding="utf-8-sig").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        valores[clave.strip()] = valor.strip().strip('"').strip("'")

    # Una variable del entorno de Windows gana sobre el .env, para poder
    # probar sin editar el archivo.
    for clave in ("SQLPLUS_EXE", "ORACLE_USUARIO", "ORACLE_PASSWORD", "ORACLE_ALIAS"):
        if os.environ.get(clave):
            valores[clave] = os.environ[clave]

    valores["ESQUEMAS"] = valores.get("ESQUEMAS", "").strip() or ",".join(
        f"{esquema}:{empresa}" for esquema, empresa in ESQUEMAS_POR_OMISION.items()
    )

    faltan = [c for c in ("ORACLE_USUARIO", "ORACLE_ALIAS") if not valores.get(c)]
    if faltan:
        sys.exit(f"Falta completar {', '.join(faltan)} en {ARCHIVO_ENV}")

    sqlplus = valores.get("SQLPLUS_EXE", "")
    if not sqlplus:
        sys.exit(f"Falta SQLPLUS_EXE en {ARCHIVO_ENV}")
    if not Path(sqlplus).exists():
        sys.exit(
            f"No se encontro SQL*Plus en:\n  {sqlplus}\n\n"
            f"Corregi SQLPLUS_EXE en {ARCHIVO_ENV}."
        )
    return valores


def esquemas_configurados(config: dict[str, str]) -> dict[str, str]:
    """{ESQUEMA: nombre de la sociedad}, en el orden del .env."""
    esquemas: dict[str, str] = {}
    for parte in config["ESQUEMAS"].split(","):
        esquema, _, empresa = parte.partition(":")
        esquema = esquema.strip().upper()
        if not esquema:
            continue
        # El nombre del esquema se interpola en el SQL, asi que solo se acepta
        # un identificador de Oracle.
        if not esquema.replace("_", "").isalnum():
            sys.exit(f"Nombre de esquema invalido en ESQUEMAS: {esquema!r}")
        esquemas[esquema] = empresa.strip() or esquema
    if not esquemas:
        sys.exit(f"ESQUEMAS quedo vacio en {ARCHIVO_ENV}")
    return esquemas


def elegir_esquemas(config: dict[str, str], pedidos: str | None) -> dict[str, str]:
    """Los esquemas a consultar: los de --esquema, o todos."""
    disponibles = esquemas_configurados(config)
    if not pedidos or pedidos.strip().lower() in ("todos", "todas", "*"):
        return disponibles

    elegidos: dict[str, str] = {}
    for nombre in pedidos.split(","):
        esquema = nombre.strip().upper()
        if not esquema:
            continue
        if esquema not in disponibles:
            sys.exit(
                f"El esquema {esquema} no esta en ESQUEMAS del .env.\n"
                f"Disponibles: {', '.join(disponibles)}"
            )
        elegidos[esquema] = disponibles[esquema]
    if not elegidos:
        sys.exit("--esquema quedo vacio")
    return elegidos


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------

def generar_sql(esquema: str, desde: datetime, hasta: datetime, planta: str | None, especie: str) -> str:
    filtro_planta = f"\n  AND I.IPL_PLANTA = {int(planta)}" if planta else ""
    return f"""
WHENEVER OSERROR EXIT 10
WHENEVER SQLERROR EXIT SQL.SQLCODE
SET ECHO OFF
SET FEEDBACK OFF
SET VERIFY OFF
SET HEADING OFF
SET PAGESIZE 0
SET LINESIZE 32767
SET TRIMSPOOL ON
SET TERMOUT ON
SET SQLBLANKLINES ON
SET DEFINE OFF

SELECT {SELECT}
FROM {esquema}.INGRESOS_PLANTAS I
WHERE I.IPL_ESPECIE = '{especie}'
  AND I.IPL_FECHA_HORA >= TO_DATE('{desde:%Y-%m-%d %H:%M:%S}', 'YYYY-MM-DD HH24:MI:SS')
  AND I.IPL_FECHA_HORA <  TO_DATE('{hasta:%Y-%m-%d %H:%M:%S}', 'YYYY-MM-DD HH24:MI:SS')
  AND I.IPL_CTG IS NOT NULL
  AND I.IPL_PESO_NETO IS NOT NULL
  AND I.IPL_PESO_NETO > 0{filtro_planta}
ORDER BY I.IPL_FECHA_HORA, I.IPL_PUNTO_INGRESO, I.IPL_NUMERO_INGRESO;
EXIT 0
"""


def ejecutar(config: dict[str, str], esquema: str, empresa: str, sql: str) -> list[dict[str, str]]:
    """Corre la consulta en un esquema y devuelve sus filas."""
    archivo_sql: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False,
                                         encoding="latin-1", newline="\n") as archivo:
            archivo.write(sql)
            archivo_sql = Path(archivo.name)

        comandos = (
            f'CONNECT {config["ORACLE_USUARIO"]}/"{config.get("ORACLE_PASSWORD", "")}"'
            f'@{config["ORACLE_ALIAS"]}\n@{archivo_sql}\n'
        )
        resultado = subprocess.run(
            [config["SQLPLUS_EXE"], "-L", "-S", "/nolog"], input=comandos,
            capture_output=True, text=True, encoding="latin-1", errors="replace", timeout=180,
        )
        salida, error = resultado.stdout or "", resultado.stderr or ""
        completa = f"{salida}\n{error}".upper()
        if resultado.returncode != 0 or "ORA-" in completa or "SP2-" in completa:
            sys.exit(
                f"Error de SQL*Plus consultando {esquema} (codigo {resultado.returncode}):\n"
                f"{salida}\n{error}"
            )

        registros = []
        for linea in salida.splitlines():
            linea = linea.strip()
            if "|" not in linea:
                continue
            valores = [v.strip() for v in linea.split("|")]
            if len(valores) != len(CAMPOS_SQL):
                print(f"ADVERTENCIA: fila ignorada ({len(valores)} campos, esperados {len(CAMPOS_SQL)})")
                continue
            registro = dict(zip(CAMPOS_SQL, valores))
            registro["empresa"] = empresa
            registro["esquema"] = esquema
            registros.append(registro)
        return registros
    finally:
        if archivo_sql and archivo_sql.exists():
            archivo_sql.unlink()


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="CTG de ingresos de soja para informar a Visec")
    ap.add_argument("--desde", type=date.fromisoformat, help="AAAA-MM-DD (por omision, hoy)")
    ap.add_argument("--hasta", type=date.fromisoformat, help="AAAA-MM-DD, inclusive (por omision, igual que desde)")
    ap.add_argument("--esquema",
                    help="Esquema a consultar, o varios separados por coma "
                         "(por omision, todos los de ESQUEMAS del .env)")
    ap.add_argument("--planta", help="Filtrar por IPL_PLANTA")
    ap.add_argument("--especie", default=ESPECIE_SOJA,
                    help=f"Codigo interno de especie (por omision {ESPECIE_SOJA} = soja)")
    ap.add_argument("-o", "--salida", type=Path, help="Nombre del CSV (por omision, en esta misma carpeta)")
    args = ap.parse_args()

    config = leer_env()

    desde = args.desde or date.today()
    hasta = args.hasta or desde
    if hasta < desde:
        ap.error("--hasta no puede ser anterior a --desde")

    esquemas = elegir_esquemas(config, args.esquema)
    desde_sql = datetime.combine(desde, datetime.min.time())
    hasta_sql = datetime.combine(hasta + timedelta(days=1), datetime.min.time())

    print(f"Ingresos de especie {args.especie} del {desde:%d/%m/%Y} al {hasta:%d/%m/%Y}\n")
    registros: list[dict[str, str]] = []
    for esquema, empresa in esquemas.items():
        print(f"  {empresa} ({esquema})...", end=" ", flush=True)
        sql = generar_sql(esquema, desde_sql, hasta_sql, args.planta, args.especie)
        filas = ejecutar(config, esquema, empresa, sql)
        print(f"{len(filas)} CTG")
        registros.extend(filas)

    if not registros:
        print("\nNo hay ingresos con CTG para esos filtros.")
        return 0

    # Un mismo CTG no deberia aparecer en dos esquemas, pero si pasa se informa
    # una sola vez: Visec lo rechazaria por duplicado.
    vistos: set[str] = set()
    unicos = []
    for registro in registros:
        if registro["ctg"] in vistos:
            print(f"ADVERTENCIA: CTG {registro['ctg']} repetido ({registro['esquema']}), se omite")
            continue
        vistos.add(registro["ctg"])
        unicos.append(registro)
    registros = unicos

    sufijo = f"_{'_'.join(esquemas)}" if len(esquemas) == 1 else ""
    salida = args.salida or BASE_DIR / f"ctg_soja_{desde:%Y%m%d}_{hasta:%Y%m%d}{sufijo}.csv"
    if not salida.is_absolute():
        salida = BASE_DIR / salida
    # utf-8-sig para que Excel abra bien los acentos al hacer doble clic.
    with salida.open("w", newline="", encoding="utf-8-sig") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=COLUMNAS, delimiter=";")
        escritor.writeheader()
        escritor.writerows(registros)

    print(f"\nListo: {len(registros)} CTG en total")
    print(f"Archivo: {salida}")
    print("\nSubi ese archivo en la app 'Informar CPE a Visec'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
