#!/usr/bin/env python3
"""Genera el XLSX de "Registración de Cartas de Porte" de Visec a partir de
SYSADMIN_ELN.INGRESOS_PLANTAS (Oracle, vía SQL*Plus, igual que cpe_bolsatech.py).

Uso:
  python generar_xls_visec.py --desde 2026-10-01
  python generar_xls_visec.py --desde 2026-10-01 --hasta 2026-10-07
  python generar_xls_visec.py --desde 2026-10-01 --planta 2 --tipos E,C

Salida (carpeta salida/):
  visec_cpe_AAAAMMDD_AAAAMMDD.xlsx         -> para subir a Visec
  visec_cpe_AAAAMMDD_AAAAMMDD_avisos.txt   -> datos que faltaron, por CTG
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path

from dotenv import load_dotenv
from openpyxl import load_workbook

BASE_DIR = Path(__file__).resolve().parent
PLANTILLA = BASE_DIR / "plantilla_visec.xlsx"
SALIDA_DIR = BASE_DIR / "salida"


# ---------------------------------------------------------------------------
# Configuración
# ---------------------------------------------------------------------------

def mapa_env(nombre: str) -> dict[str, str]:
    """Lee un mapa 'a:b,c:d' desde una variable de entorno."""
    salida = {}
    for par in os.getenv(nombre, "").split(","):
        if ":" in par:
            clave, valor = par.split(":", 1)
            salida[clave.strip()] = valor.strip()
    return salida


def obtener_configuracion() -> dict:
    load_dotenv(BASE_DIR / ".env")
    config = {
        "sqlplus_exe": os.getenv("SQLPLUS_EXE", r"D:\oracle\product\11.2.0\client_1\bin\sqlplus.exe").strip(),
        "oracle_usuario": os.getenv("ORACLE_USUARIO", "").strip(),
        "oracle_password": os.getenv("ORACLE_PASSWORD", "").strip(),
        "oracle_alias": os.getenv("ORACLE_ALIAS", "BASE").strip(),
        # numero_planta_oncca:cuit  (planta sin mapear -> CUIT_EMPRESA)
        "cuit_empresa": os.getenv("CUIT_EMPRESA", "30601191640").strip(),
        "cuit_por_planta": mapa_env("MAPA_PLANTA_CUIT"),
        "especies": mapa_env("MAPA_ESPECIE_PRODUCTO"),
        "tipo_movimiento": mapa_env("MAPA_TIPO_MOVIMIENTO"),
        "tipos_ingreso": os.getenv("TIPOS_INGRESO", "").strip(),
        "formato_fecha_hora": os.getenv("FORMATO_FECHA_HORA", "%d/%m/%Y %H:%M"),
        "formato_fecha": os.getenv("FORMATO_FECHA", "%d/%m/%Y"),
    }
    if not Path(config["sqlplus_exe"]).exists():
        sys.exit(f"No se encontró SQL*Plus en: {config['sqlplus_exe']}")
    if not config["oracle_usuario"] or not config["oracle_alias"]:
        sys.exit("Falta ORACLE_USUARIO u ORACLE_ALIAS en .env")
    if len(cuit(config["cuit_empresa"])) != 11:
        sys.exit("CUIT_EMPRESA inválido en .env")
    return config


# ---------------------------------------------------------------------------
# Consulta Oracle
# ---------------------------------------------------------------------------

# Columnas que devuelve la consulta, en orden. Para sumar un dato nuevo
# (ej. CUIT del corredor) agregalo acá y en SELECT_COLUMNAS con el mismo orden.
COLUMNAS = [
    "punto_ingreso", "numero_ingreso", "tipo_ingreso", "especie", "cosecha",
    "fecha_hora", "fecha_hora_carga", "fecha_hora_conf_arribo",
    "sucursal_cpe", "numero_cpe", "ctg",
    "cuit_productor", "cyo1_cuit", "cyo2_cuit", "corredor", "renspa",
    "planta", "numero_planta_oncca",
    "peso_neto_productor", "peso_neto",
]

SELECT_COLUMNAS = """
    NVL(TRIM(TO_CHAR(I.IPL_PUNTO_INGRESO)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_NUMERO_INGRESO)), '') || '|' ||
    NVL(TRIM(I.IPL_TIPO_INGRESO), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_ESPECIE)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_COSECHA)), '') || '|' ||
    TO_CHAR(I.IPL_FECHA_HORA, 'YYYY-MM-DD HH24:MI:SS') || '|' ||
    TO_CHAR(I.IPL_FECHA_HORA_CARGA, 'YYYY-MM-DD HH24:MI:SS') || '|' ||
    TO_CHAR(I.IPL_FECHA_HORA_CONF_ARRIBO, 'YYYY-MM-DD HH24:MI:SS') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_SUCURSAL_INTERNA_CPE)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_NUMERO_INTERNO_CPE)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_CTG)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_CUIT_PRODUCTOR)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_CYO1_CUIT)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_CYO2_CUIT)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_CORREDOR)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_NUMERO_RENSPA)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_PLANTA)), '') || '|' ||
    NVL(TRIM(TO_CHAR(P.PLA_NUMERO_ONCCA)), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_PESO_NETO_PRODUCTOR, 'FM999999999999990D999', 'NLS_NUMERIC_CHARACTERS=''.,''')), '') || '|' ||
    NVL(TRIM(TO_CHAR(I.IPL_PESO_NETO, 'FM999999999999990D999', 'NLS_NUMERIC_CHARACTERS=''.,''')), '')
"""


def generar_sql(desde: datetime, hasta: datetime, planta: str | None, tipos: list[str]) -> str:
    filtro_planta = f"\n  AND I.IPL_PLANTA = {int(planta)}" if planta else ""
    filtro_tipos = ""
    if tipos:
        lista = ", ".join(f"'{t}'" for t in tipos)
        filtro_tipos = f"\n  AND I.IPL_TIPO_INGRESO IN ({lista})"
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

SELECT {SELECT_COLUMNAS}
FROM SYSADMIN_ELN.INGRESOS_PLANTAS I
LEFT JOIN SYSADMIN_ELN.PLANTAS P ON P.PLA_PLANTA = I.IPL_PLANTA
WHERE I.IPL_FECHA_HORA >= TO_DATE('{desde:%Y-%m-%d %H:%M:%S}', 'YYYY-MM-DD HH24:MI:SS')
  AND I.IPL_FECHA_HORA <  TO_DATE('{hasta:%Y-%m-%d %H:%M:%S}', 'YYYY-MM-DD HH24:MI:SS')
  AND I.IPL_CTG IS NOT NULL{filtro_planta}{filtro_tipos}
ORDER BY I.IPL_FECHA_HORA, I.IPL_PUNTO_INGRESO, I.IPL_NUMERO_INGRESO;
EXIT 0
"""


def ejecutar_consulta(config: dict, sql: str) -> list[dict[str, str]]:
    archivo_sql: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".sql", delete=False,
                                         encoding="latin-1", newline="\n") as archivo:
            archivo.write(sql)
            archivo_sql = Path(archivo.name)
        comandos = (f'CONNECT {config["oracle_usuario"]}/"{config["oracle_password"]}"'
                    f'@{config["oracle_alias"]}\n@{archivo_sql}\n')
        resultado = subprocess.run(
            [config["sqlplus_exe"], "-L", "-S", "/nolog"], input=comandos,
            capture_output=True, text=True, encoding="latin-1", errors="replace", timeout=180,
        )
        salida, error = resultado.stdout or "", resultado.stderr or ""
        completa = f"{salida}\n{error}".upper()
        if resultado.returncode != 0 or "ORA-" in completa or "SP2-" in completa:
            sys.exit(f"Error de SQL*Plus ({resultado.returncode}):\n{salida}\n{error}")
        registros = []
        for linea in salida.splitlines():
            linea = linea.strip()
            if "|" not in linea:
                continue
            valores = [v.strip() for v in linea.split("|")]
            if len(valores) != len(COLUMNAS):
                print(f"ADVERTENCIA: fila ignorada ({len(valores)} columnas, esperadas {len(COLUMNAS)})")
                continue
            registros.append(dict(zip(COLUMNAS, valores)))
        return registros
    finally:
        if archivo_sql and archivo_sql.exists():
            archivo_sql.unlink()


# ---------------------------------------------------------------------------
# Formatos
# ---------------------------------------------------------------------------

def cuit(valor: str) -> str:
    """CUIT solo dígitos; '' si no tiene 11 dígitos (ej. '0')."""
    limpio = "".join(c for c in (valor or "") if c.isdigit())
    return limpio if len(limpio) == 11 else ""


def kilos(valor: str) -> str:
    if not valor:
        return ""
    try:
        numero = Decimal(valor.replace(",", "."))
    except InvalidOperation:
        return ""
    return str(int(numero.quantize(Decimal("1"), rounding=ROUND_HALF_UP))) if numero > 0 else ""


def formatear_fecha(valor: str, formato: str) -> str:
    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S").strftime(formato)
    except ValueError:
        return ""


# ---------------------------------------------------------------------------
# Mapeo a columnas Visec
# ---------------------------------------------------------------------------

COLUMNAS_VISEC = [
    "Fecha y Hora Movimiento", "Fecha CPE", "Número CPE", "Número CTG",
    "CUIT Titular", "Número RUCA Origen", "CUIT Remitente Comercial Productor",
    "CUIT Rte Comercial Venta Primaria", "CUIT Rte Comercial Venta Secundaria",
    "CUIT Rte Comercial Venta Secundaria 2", "CUIT Corredor Venta Primaria",
    "CUIT Corredor Venta Secundaria", "CUIT Destinatario", "CUIT Destino",
    "Número RUCA Destino", "Código Producto", "Campaña", "Peso Neto Carga (Kg)",
    "Número RENSPA", "Número CTG Asignado", "Peso Neto Carga (Kg) por UP",
    "Peso Neto Descarga (Kg) por UP", "Peso Ingreso Stock (Kg)",
    "Último Almacenamiento", "Tipo Movimiento",
]


def mapear(r: dict[str, str], config: dict, avisos: list[str]) -> dict[str, str]:
    ref = f"CTG {r['ctg']} (ingreso {r['punto_ingreso']}-{r['numero_ingreso']})"

    def falta(campo: str) -> None:
        avisos.append(f"{ref}: falta {campo}")

    planta_oncca = r["numero_planta_oncca"]
    cuit_destino = cuit(config["cuit_por_planta"].get(planta_oncca, "")) or cuit(config["cuit_empresa"])

    # Mismo formato que cpe_bolsatech.py: sucursal + número interno a 8 dígitos
    numero_cpe = (r["sucursal_cpe"] + r["numero_cpe"].zfill(8)) if r["sucursal_cpe"] and r["numero_cpe"] else ""

    # Titular: el productor; en transferencias propias (sin productor) la planta destino
    titular = cuit(r["cuit_productor"]) or (cuit_destino if r["tipo_ingreso"] == "T" else "")

    fila = {
        "Fecha y Hora Movimiento": formatear_fecha(r["fecha_hora_conf_arribo"] or r["fecha_hora"], config["formato_fecha_hora"]),
        "Fecha CPE": formatear_fecha(r["fecha_hora_carga"] or r["fecha_hora"], config["formato_fecha"]),
        "Número CPE": numero_cpe,
        "Número CTG": r["ctg"],
        "CUIT Titular": titular,
        "Número RUCA Origen": "",
        "CUIT Remitente Comercial Productor": "",
        "CUIT Rte Comercial Venta Primaria": cuit(r["cyo1_cuit"]),
        "CUIT Rte Comercial Venta Secundaria": cuit(r["cyo2_cuit"]),
        "CUIT Rte Comercial Venta Secundaria 2": "",
        "CUIT Corredor Venta Primaria": "",
        "CUIT Corredor Venta Secundaria": "",
        "CUIT Destinatario": cuit_destino,
        "CUIT Destino": cuit_destino,
        "Número RUCA Destino": planta_oncca,
        "Código Producto": config["especies"].get(r["especie"], ""),
        "Campaña": r["cosecha"],
        "Peso Neto Carga (Kg)": kilos(r["peso_neto_productor"]),
        "Número RENSPA": r["renspa"],
        "Número CTG Asignado": "",
        "Peso Neto Carga (Kg) por UP": "",
        "Peso Neto Descarga (Kg) por UP": "",
        "Peso Ingreso Stock (Kg)": kilos(r["peso_neto"]),
        "Último Almacenamiento": "",
        "Tipo Movimiento": config["tipo_movimiento"].get(r["tipo_ingreso"], ""),
    }

    if not fila["Número CPE"]:
        falta("Número CPE (IPL_SUCURSAL_INTERNA_CPE / IPL_NUMERO_INTERNO_CPE vacíos)")
    if not fila["CUIT Titular"]:
        falta("CUIT Titular")
    if not fila["Número RUCA Destino"]:
        falta(f"RUCA Destino (PLA_NUMERO_ONCCA vacío para planta {r['planta']})")
    if not fila["Código Producto"]:
        falta(f"Código Producto (especie '{r['especie']}' sin mapear en MAPA_ESPECIE_PRODUCTO)")
    if not fila["Peso Neto Carga (Kg)"]:
        falta("Peso Neto Carga (IPL_PESO_NETO_PRODUCTOR vacío)")
    if r["corredor"] and not fila["CUIT Corredor Venta Primaria"]:
        falta(f"CUIT Corredor (código corredor {r['corredor']})")
    if not fila["Tipo Movimiento"]:
        falta(f"Tipo Movimiento (tipo ingreso '{r['tipo_ingreso']}' sin mapear en MAPA_TIPO_MOVIMIENTO)")
    return fila


# ---------------------------------------------------------------------------
# Escritura sobre la plantilla
# ---------------------------------------------------------------------------

def escribir_xlsx(filas: list[dict[str, str]], destino: Path) -> None:
    wb = load_workbook(PLANTILLA)
    ws = wb["Cartas de Porte"]
    encabezados = [ws.cell(1, i + 1).value for i in range(len(COLUMNAS_VISEC))]
    if encabezados != COLUMNAS_VISEC:
        sys.exit(f"La plantilla no tiene las columnas esperadas:\n{encabezados}")
    if ws.max_row > 1:
        ws.delete_rows(2, ws.max_row - 1)
    formatos = [ws.cell(1, i + 1).number_format for i in range(len(COLUMNAS_VISEC))]
    for n, fila in enumerate(filas, start=2):
        for i, columna in enumerate(COLUMNAS_VISEC):
            celda = ws.cell(n, i + 1, fila[columna] or None)
            celda.number_format = formatos[i]  # '@' = texto, como la plantilla
    wb.save(destino)


# ---------------------------------------------------------------------------

def main() -> int:
    config = obtener_configuracion()

    ap = argparse.ArgumentParser(description="Genera el XLSX de CPE para Visec")
    ap.add_argument("--desde", type=date.fromisoformat, required=True, help="AAAA-MM-DD")
    ap.add_argument("--hasta", type=date.fromisoformat, help="AAAA-MM-DD, inclusive (default = desde)")
    ap.add_argument("--planta", help="Filtrar por IPL_PLANTA (código interno)")
    ap.add_argument("--tipos", default=config["tipos_ingreso"], help="Tipos de ingreso, ej: E,C,T (vacío = todos)")
    ap.add_argument("-o", "--salida", type=Path, help="Archivo .xlsx de salida")
    args = ap.parse_args()

    hasta = args.hasta or args.desde
    if hasta < args.desde:
        ap.error("--hasta no puede ser anterior a --desde")
    tipos = [t.strip().upper() for t in args.tipos.split(",") if t.strip().isalpha()]

    sql = generar_sql(
        datetime.combine(args.desde, datetime.min.time()),
        datetime.combine(hasta + timedelta(days=1), datetime.min.time()),
        args.planta, tipos,
    )
    registros = ejecutar_consulta(config, sql)
    if not registros:
        print("No hay ingresos con CTG para los filtros indicados.")
        return 0

    avisos: list[str] = []
    filas = [mapear(r, config, avisos) for r in registros]

    salida = args.salida or SALIDA_DIR / f"visec_cpe_{args.desde:%Y%m%d}_{hasta:%Y%m%d}.xlsx"
    salida.parent.mkdir(parents=True, exist_ok=True)
    escribir_xlsx(filas, salida)
    print(f"OK: {len(filas)} filas -> {salida}")

    if avisos:
        ruta_avisos = salida.with_name(salida.stem + "_avisos.txt")
        ruta_avisos.write_text("\n".join(avisos) + "\n", encoding="utf-8")
        print(f"ATENCIÓN: {len(avisos)} avisos -> {ruta_avisos}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
