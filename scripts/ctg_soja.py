#!/usr/bin/env python3
"""Lista los CTG de los ingresos de soja (especie 38) para informar a Visec.

Pensado para copiarse a una carpeta cualquiera del servidor que tiene SQL*Plus
(por ejemplo el Escritorio) y correrse ahi: lee el .env y escribe el CSV en la
misma carpeta donde esta este archivo.

No necesita instalar nada: usa solo la biblioteca estandar de Python 3.8+.

Consulta SYSADMIN_ELN.INGRESOS_PLANTAS igual que cpe_bolsatech.py, y ademas del
CTG exporta la fecha del movimiento y el peso neto de balanza, que son los dos
datos que ARCA no tiene. El CSV resultante se sube a la app "Informar CPE a
Visec".

Uso:
  python ctg_soja.py                          (los ingresos de hoy)
  python ctg_soja.py --desde 2026-10-01
  python ctg_soja.py --desde 2026-10-01 --hasta 2026-10-07
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
"""

COLUMNAS = [
    "ctg", "numero_cpe", "fecha_movimiento", "peso_ingreso_stock",
    "punto_ingreso", "numero_ingreso", "planta", "cosecha", "cuit_productor",
]

# El orden de los campos tiene que coincidir con COLUMNAS.
SELECT = """
    NVL(TRIM(TO_CHAR(I.IPL_CTG)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_SUCURSAL_INTERNA_CPE)), '') || LPAD(NVL(TRIM(TO_CHAR(I.IPL_NUMERO_INTERNO_CPE)), ''), 8, '0') || '|' ||
    TO_CHAR(NVL(I.IPL_FECHA_HORA_CONF_ARRIBO, I.IPL_FECHA_HORA), 'YYYY-MM-DD HH24:MI:SS') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_PESO_NETO, 'FM999999999999990', 'NLS_NUMERIC_CHARACTERS=''.,''')), '') || '|' ||
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


# ---------------------------------------------------------------------------
# Consulta
# ---------------------------------------------------------------------------

def generar_sql(desde: datetime, hasta: datetime, planta: str | None, especie: str) -> str:
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
FROM SYSADMIN_ELN.INGRESOS_PLANTAS I
WHERE I.IPL_ESPECIE = '{especie}'
  AND I.IPL_FECHA_HORA >= TO_DATE('{desde:%Y-%m-%d %H:%M:%S}', 'YYYY-MM-DD HH24:MI:SS')
  AND I.IPL_FECHA_HORA <  TO_DATE('{hasta:%Y-%m-%d %H:%M:%S}', 'YYYY-MM-DD HH24:MI:SS')
  AND I.IPL_CTG IS NOT NULL
  AND I.IPL_PESO_NETO IS NOT NULL
  AND I.IPL_PESO_NETO > 0{filtro_planta}
ORDER BY I.IPL_FECHA_HORA, I.IPL_PUNTO_INGRESO, I.IPL_NUMERO_INGRESO;
EXIT 0
"""


def ejecutar(config: dict[str, str], sql: str) -> list[dict[str, str]]:
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
            sys.exit(f"Error de SQL*Plus (codigo {resultado.returncode}):\n{salida}\n{error}")

        registros = []
        for linea in salida.splitlines():
            linea = linea.strip()
            if "|" not in linea:
                continue
            valores = [v.strip() for v in linea.split("|")]
            if len(valores) != len(COLUMNAS):
                print(f"ADVERTENCIA: fila ignorada ({len(valores)} campos, esperados {len(COLUMNAS)})")
                continue
            registros.append(dict(zip(COLUMNAS, valores)))
        return registros
    finally:
        if archivo_sql and archivo_sql.exists():
            archivo_sql.unlink()


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="CTG de ingresos de soja para informar a Visec")
    ap.add_argument("--desde", type=date.fromisoformat, help="AAAA-MM-DD (por omision, hoy)")
    ap.add_argument("--hasta", type=date.fromisoformat, help="AAAA-MM-DD, inclusive (por omision, igual que desde)")
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

    print(f"Consultando ingresos de especie {args.especie} del {desde:%d/%m/%Y} al {hasta:%d/%m/%Y}...")
    registros = ejecutar(config, generar_sql(
        datetime.combine(desde, datetime.min.time()),
        datetime.combine(hasta + timedelta(days=1), datetime.min.time()),
        args.planta, args.especie,
    ))
    if not registros:
        print("No hay ingresos con CTG para esos filtros.")
        return 0

    salida = args.salida or BASE_DIR / f"ctg_soja_{desde:%Y%m%d}_{hasta:%Y%m%d}.csv"
    if not salida.is_absolute():
        salida = BASE_DIR / salida
    # utf-8-sig para que Excel abra bien los acentos al hacer doble clic.
    with salida.open("w", newline="", encoding="utf-8-sig") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=COLUMNAS, delimiter=";")
        escritor.writeheader()
        escritor.writerows(registros)

    print(f"\nListo: {len(registros)} CTG")
    print(f"Archivo: {salida}")
    print("\nSubi ese archivo en la app 'Informar CPE a Visec'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
