"""SoftCereal -> Albor: arma la planilla de importación de descargas de Albor
(DESCARGA_CP_Albor.xlsx, hoja "Padron") a partir de la exportación de ingresos
a planta de SoftCereal.

Las reglas salen de la hoja "Referencias" de la propia plantilla de Albor:

  C(x)    texto de hasta x caracteres
  N(x)    entero de hasta x dígitos
  D(x,y)  número de hasta x dígitos, y de ellos decimales

  Tipo CP E: hace falta el CTG, o si no, Sucursal + CP.
  Tipo CP M: hace falta el CP.

Una fila que no cumple no va al archivo: Albor rechaza la importación entera
por una fila mal armada, así que es mejor dejarla afuera y mostrarla.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

from openpyxl import load_workbook

from .config import BASE_DIR

PLANTILLA_ALBOR = BASE_DIR / "plantilla_albor.xlsx"
HOJA = "Padron"

# (columna de Albor, formato de la hoja Referencias)
COLUMNAS = [
    ("Tipo CP", "C1"),
    ("Sucursal CPE", "N5"),
    ("CTG", "N20"),
    ("CP", "N15"),
    ("Flete Corto", "C1"),
    ("Bruto Destino", "N8"),
    ("Tara Destino", "N8"),
    ("Porcentaje Humedad destino", "D4,2"),
    ("Merma Humedad", "D8,2"),
    ("Porcentaje Zaranda", "D4,2"),
    ("KG Zaranda", "D8,2"),
    ("Merma Kg Volatil", "D6,4"),
    ("Kg Volatil", "D8,2"),
    ("Otras mermas", "D8,2"),
    ("Factor", "D8,3"),
    ("Observaciones", "C250"),
]
NOMBRES = [nombre for nombre, _ in COLUMNAS]


# ---------------------------------------------------------------------------
# Columnas de la exportación de SoftCereal
# ---------------------------------------------------------------------------

def _normalizar(nombre: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", nombre).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "_", sin_tildes.strip().lower()).strip("_")


# Campo interno -> nombres posibles del encabezado de SoftCereal (normalizados).
# "Sucursar Interna CPE" viene así, con el error de tipeo, del ERP.
FUENTES = {
    "ctg": ["ctg"],
    "sucursal": ["sucursar_interna_cpe", "sucursal_interna_cpe"],
    "numero_cpe": ["numero_interno_cpe"],
    "bruto": ["peso_bruto_reconocido", "peso_bruto_acopio"],
    "tara": ["tara"],
    "humedad": ["humedad"],
    "kg_humedad": ["kilos_de_merma_humedad"],
    "pct_zaranda": ["merma_zarandeo"],
    "kg_zaranda": ["kilos_merma_zarandeo"],
    "pct_volatil": ["merma_volatil"],
    "kg_volatil": ["kilos_merma_volatil"],
    "factor": ["factor"],
    "ingreso": ["numero_ingreso"],
    "observaciones": ["observaciones_orden_de_carga"],
}
# Sin estas no se puede armar ninguna fila.
OBLIGATORIAS = ["ctg", "bruto", "tara"]


class ErrorArchivo(ValueError):
    pass


def _indices(encabezado: list[str]) -> dict[str, int]:
    """Campo interno -> índice de columna. Toma la primera coincidencia: la
    exportación trae encabezados repetidos ("Tarifa Fumigada")."""
    posiciones: dict[str, int] = {}
    for indice, nombre in enumerate(encabezado):
        posiciones.setdefault(_normalizar(nombre), indice)
    indices = {}
    for campo, candidatos in FUENTES.items():
        for candidato in candidatos:
            if candidato in posiciones:
                indices[campo] = posiciones[candidato]
                break
    faltan = [c for c in OBLIGATORIAS if c not in indices]
    if faltan:
        nombres = {"ctg": "CTG", "bruto": "Peso Bruto Reconocido", "tara": "Tara"}
        raise ErrorArchivo(
            "El archivo no parece una exportación de ingresos de SoftCereal: falta la columna "
            + ", ".join(nombres[c] for c in faltan)
        )
    return indices


# ---------------------------------------------------------------------------
# Formatos de Albor
# ---------------------------------------------------------------------------

def _decimal(texto: str) -> Decimal | None:
    texto = (texto or "").strip().replace(",", ".")
    if not texto:
        return None
    try:
        return Decimal(texto)
    except InvalidOperation:
        return None


def _formatear(valor, formato: str):
    """Devuelve (valor listo para la celda, error o '')."""
    if valor in (None, ""):
        return None, ""
    tipo, medida = formato[0], formato[1:]
    if tipo == "C":
        texto = str(valor).strip()
        largo = int(medida)
        if len(texto) <= largo:
            return texto, ""
        if largo == 1:  # un código (E/M, S/N): recortarlo cambiaría su significado
            return None, f"'{texto}' tiene más de {largo} carácter"
        return texto[:largo], ""  # observaciones: se recortan
    numero = valor if isinstance(valor, Decimal) else _decimal(str(valor))
    if numero is None:
        return None, f"'{valor}' no es un número"
    if numero < 0:
        return None, "es negativo"
    if tipo == "N":
        entero = numero.to_integral_value(rounding=ROUND_HALF_UP)
        if len(str(int(entero))) > int(medida):
            return None, f"tiene más de {medida} dígitos"
        return int(entero), ""
    digitos, decimales = (int(x) for x in medida.split(","))
    redondeado = numero.quantize(Decimal(1).scaleb(-decimales), rounding=ROUND_HALF_UP)
    if len(str(int(redondeado))) > digitos - decimales:
        return None, f"supera el máximo de {digitos - decimales} dígitos enteros"
    return float(redondeado), ""


# ---------------------------------------------------------------------------
# Conversión
# ---------------------------------------------------------------------------

@dataclass
class Fila:
    numero: int           # fila de la planilla de SoftCereal, para encontrarla
    ingreso: str
    valores: dict = field(default_factory=dict)
    errores: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errores


def _fila(numero: int, datos: list[str], indices: dict[str, int]) -> Fila:
    def dato(campo: str) -> str:
        i = indices.get(campo)
        return datos[i].strip() if i is not None and i < len(datos) else ""

    ctg = re.sub(r"\D", "", dato("ctg"))
    sucursal, cp = dato("sucursal"), dato("numero_cpe")
    observaciones = " · ".join(p for p in (
        f"Ingreso {dato('ingreso')}" if dato("ingreso") else "",
        dato("observaciones"),
    ) if p)

    crudos = {
        # Toda CPE de SoftCereal es electrónica: las manuales dejaron de
        # existir en 2021. Sin CTG ni CPE no hay como identificarla.
        "Tipo CP": "E",
        "Sucursal CPE": sucursal,
        "CTG": ctg,
        "CP": cp,
        "Flete Corto": "N",
        "Bruto Destino": dato("bruto"),
        "Tara Destino": dato("tara"),
        "Porcentaje Humedad destino": dato("humedad"),
        "Merma Humedad": dato("kg_humedad"),
        "Porcentaje Zaranda": dato("pct_zaranda"),
        "KG Zaranda": dato("kg_zaranda"),
        "Merma Kg Volatil": dato("pct_volatil"),
        "Kg Volatil": dato("kg_volatil"),
        "Otras mermas": "",
        "Factor": dato("factor"),
        "Observaciones": observaciones,
    }

    fila = Fila(numero=numero, ingreso=dato("ingreso"))
    for nombre, formato in COLUMNAS:
        valor, error = _formatear(crudos[nombre], formato)
        if error:
            fila.errores.append(f"{nombre}: {error}")
        fila.valores[nombre] = valor

    # Obligatoriedad (hoja Referencias): Tipo E necesita CTG, o Sucursal + CP.
    v = fila.valores
    if not v["CTG"] and not (v["Sucursal CPE"] and v["CP"]):
        fila.errores.append("Falta el CTG, y tampoco hay Sucursal + CP para identificar la CPE")
    if v["Bruto Destino"] is None or v["Tara Destino"] is None:
        fila.errores.append("Falta el peso bruto o la tara de destino")
    elif v["Tara Destino"] >= v["Bruto Destino"]:
        fila.errores.append(f"La tara ({v['Tara Destino']}) no es menor que el bruto ({v['Bruto Destino']})")
    return fila


@dataclass
class Resultado:
    filas: list[Fila]
    repetidos: list[str]

    @property
    def validas(self) -> list[Fila]:
        return [f for f in self.filas if f.ok]

    @property
    def con_error(self) -> list[Fila]:
        return [f for f in self.filas if not f.ok]


def convertir(filas_planilla: list[list[str]]) -> Resultado:
    """Filas de SoftCereal (con el encabezado primero) -> filas de Albor."""
    if len(filas_planilla) < 2:
        raise ErrorArchivo("El archivo no tiene filas de datos")
    indices = _indices(filas_planilla[0])
    filas: list[Fila] = []
    repetidos: list[str] = []
    vistos: set = set()
    # +2: la fila 1 de SoftCereal es la de encabezados, y Excel cuenta desde 1.
    for numero, datos in enumerate(filas_planilla[1:], start=2):
        fila = _fila(numero, datos, indices)
        clave = fila.valores["CTG"] or (fila.valores["Sucursal CPE"], fila.valores["CP"])
        if fila.ok and clave in vistos:
            repetidos.append(f"CTG {fila.valores['CTG']} (fila {numero}): repetido, va una sola vez")
            continue
        vistos.add(clave)
        filas.append(fila)
    return Resultado(filas, repetidos)


def escribir(filas: list[Fila], destino: Path) -> Path:
    """Llena la hoja Padron de la plantilla de Albor. La hoja Referencias queda."""
    libro = load_workbook(PLANTILLA_ALBOR)
    hoja = libro[HOJA]
    encabezado = [hoja.cell(1, i + 1).value for i in range(len(NOMBRES))]
    if encabezado != NOMBRES:
        raise ValueError(f"La plantilla de Albor cambió de columnas: {encabezado}")
    if hoja.max_row > 1:
        hoja.delete_rows(2, hoja.max_row - 1)
    for n, fila in enumerate(filas, start=2):
        for i, nombre in enumerate(NOMBRES):
            hoja.cell(n, i + 1, fila.valores[nombre])
    destino.parent.mkdir(parents=True, exist_ok=True)
    libro.save(destino)
    return destino
