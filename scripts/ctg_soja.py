#!/usr/bin/env python3
"""Lista los CTG de los ingresos de soja (especie 38) para informar a Visec.

Corre en el servidor que tiene SQL*Plus y consulta
SYSADMIN_ELN.INGRESOS_PLANTAS, igual que cpe_bolsatech.py. No necesita más
dependencias que python-dotenv.

Genera un CSV con los CTG y los dos datos que ARCA no tiene (la fecha del
movimiento y el peso que entró a stock, que son de la balanza). Ese archivo es
el que se sube a la app "Informar CPE a Visec".

Uso:
  python ctg_soja.py --desde 2026-10-01
  python ctg_soja.py --desde 2026-10-01 --hasta 2026-10-07
  python ctg_soja.py --desde 2026-10-01 --planta 2 -o ctg.csv
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

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
ESPECIE_SOJA = "38"  # código interno del sistema; es el que Visec pide informar

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


def ejecutar(sqlplus: str, usuario: str, password: str, alias: str, sql: str) -> list[dict[str, str]]:
    archivo_sql: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False,
                                         encoding="latin-1", newline="\n") as archivo:
            archivo.write(sql)
            archivo_sql = Path(archivo.name)
        comandos = f'CONNECT {usuario}/"{password}"@{alias}\n@{archivo_sql}\n'
        resultado = subprocess.run(
            [sqlplus, "-L", "-S", "/nolog"], input=comandos, capture_output=True,
            text=True, encoding="latin-1", errors="replace", timeout=180,
        )
        salida, error = resultado.stdout or "", resultado.stderr or ""
        if resultado.returncode != 0 or "ORA-" in f"{salida}{error}".upper() or "SP2-" in f"{salida}{error}".upper():
            sys.exit(f"Error de SQL*Plus ({resultado.returncode}):\n{salida}\n{error}")

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


def main() -> int:
    load_dotenv(BASE_DIR / ".env")
    load_dotenv(BASE_DIR.parent / ".env")

    ap = argparse.ArgumentParser(description="CTG de ingresos de soja para informar a Visec")
    ap.add_argument("--desde", type=date.fromisoformat, required=True, help="AAAA-MM-DD")
    ap.add_argument("--hasta", type=date.fromisoformat, help="AAAA-MM-DD, inclusive (default = desde)")
    ap.add_argument("--planta", help="Filtrar por IPL_PLANTA")
    ap.add_argument("--especie", default=ESPECIE_SOJA, help=f"Código interno de especie (default {ESPECIE_SOJA} = soja)")
    ap.add_argument("-o", "--salida", type=Path, help="Archivo CSV de salida")
    args = ap.parse_args()

    sqlplus = os.getenv("SQLPLUS_EXE", r"D:\oracle\product\11.2.0\client_1\bin\sqlplus.exe").strip()
    usuario = os.getenv("ORACLE_USUARIO", "").strip()
    password = os.getenv("ORACLE_PASSWORD", "")
    alias = os.getenv("ORACLE_ALIAS", "BASE").strip()
    if not Path(sqlplus).exists():
        sys.exit(f"No se encontró SQL*Plus en: {sqlplus}")
    if not usuario or not alias:
        sys.exit("Falta ORACLE_USUARIO u ORACLE_ALIAS en el .env")

    hasta = args.hasta or args.desde
    if hasta < args.desde:
        ap.error("--hasta no puede ser anterior a --desde")

    sql = generar_sql(
        datetime.combine(args.desde, datetime.min.time()),
        datetime.combine(hasta + timedelta(days=1), datetime.min.time()),
        args.planta, args.especie,
    )
    registros = ejecutar(sqlplus, usuario, password, alias, sql)
    if not registros:
        print("No hay ingresos con CTG para esos filtros.")
        return 0

    salida = args.salida or BASE_DIR / f"ctg_soja_{args.desde:%Y%m%d}_{hasta:%Y%m%d}.csv"
    with salida.open("w", newline="", encoding="utf-8-sig") as archivo:
        escritor = csv.DictWriter(archivo, fieldnames=COLUMNAS, delimiter=";")
        escritor.writeheader()
        escritor.writerows(registros)

    print(f"OK: {len(registros)} CTG -> {salida}")
    print("Subí ese archivo en la app 'Informar CPE a Visec'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
