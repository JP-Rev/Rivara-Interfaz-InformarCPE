"""SoftCereal -> Albor: arma la planilla de importación de comprobantes de
cosecha de Albor (ImportacionCosecha.xlsx, hoja "Cosechas a importar") a
partir de la exportación de ingresos a planta de SoftCereal.

La plantilla la baja cada uno de Albor y se sube a la app: trae la hoja
"Referencias" con las listas de códigos de Albor (campañas, especies, cultivos,
depósitos, transportistas, choferes...). No va en el repositorio: tiene nombres
y CUIT de personas.

Lo que SoftCereal llama de una forma y Albor de otra (el lote "El Bagual-lote
15" de soja ESP 25/26 es el cultivo "02836 - El Bagual LTV 15 SOJA ESP 26/27")
se resuelve con una tabla de equivalencias: la app sugiere, el usuario confirma
una vez, y queda guardada para las próximas.
"""

from __future__ import annotations

import difflib
import io
import json
import os
import re
import unicodedata
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from xml.sax.saxutils import escape

ALBOR_DIR = Path(os.getenv("ALBOR_DIR", "/data/albor"))
PLANTILLA = ALBOR_DIR / "plantilla_cosecha.xlsx"
REFERENCIAS = ALBOR_DIR / "referencias.json"     # las listas, leídas una vez al subir la plantilla
EQUIVALENCIAS = ALBOR_DIR / "equivalencias.json"
TANDAS = ALBOR_DIR / "tandas"                    # filas leídas, esperando equivalencias
TICKETS = ALBOR_DIR / "tickets.json"             # número de ticket asignado a cada ingreso
CULTIVOS = ALBOR_DIR / "cultivos.json"           # cultivos de Albor cargados a mano
RETENCION_TANDAS = timedelta(days=2)

HOJA_DATOS = "Cosechas a importar"
HOJA_REFERENCIAS = "Referencias"

# Cómo se escriben los valores. Todas las celdas de la plantilla son texto.
# Si Albor rechaza los códigos o los decimales, se cambia en el .env.
SOLO_CODIGO = os.getenv("ALBOR_SOLO_CODIGO", "1") != "0"   # "02836" o "02836 - El Bagual..."
SEPARADOR_DECIMAL = os.getenv("ALBOR_SEPARADOR_DECIMAL", ",")
FORMATO_FECHA = os.getenv("ALBOR_FORMATO_FECHA", "%d/%m/%Y")
TIPO_FLETE = os.getenv("ALBOR_TIPO_FLETE", "T")             # T Tercero, I Interno
# El número de ticket del ERP no sirve en Albor, así que se numera aparte,
# arrancando arriba de este valor para no pisar los que ya existen.
TICKET_DESDE = int(os.getenv("ALBOR_TICKET_DESDE", "1000000"))

# Listas de la hoja Referencias que usa la conversión: nombre en la app -> columna.
LISTAS = {
    "campana": "Código campaña",
    "especie": "Código especie",
    "grano": "Tipo de Grano",
    "cultivo": "Código cultivo",
    "deposito": "Código depósito",
    "destino": "Código destino",
    "transportista": "Código transportista",
    "chofer": "Chofer (CUIT)",
    "flete": "Tipo de Flete",
}

# Equivalencias que se confirman a mano: (tipo, título, lista de Referencias, obligatoria)
TIPOS_EQUIVALENCIA = [
    ("especie", "Especie", "especie", True),
    ("cultivo", "Cultivo (campo y lote)", "cultivo", True),
    ("deposito", "Depósito destino (por planta de SoftCereal)", "deposito", True),
    ("destino", "Destino (por planta de SoftCereal)", "destino", False),
]


class ErrorArchivo(ValueError):
    pass


def _normalizar(texto: str) -> str:
    sin_tildes = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", sin_tildes.lower()).strip()


def codigo(valor: str) -> str:
    """'02836 - El Bagual LTV 15 SOJA ESP 26/27' -> '02836'."""
    return valor.split(" - ", 1)[0].strip() if " - " in valor else valor.strip()


def _valor_lista(valor: str) -> str:
    return codigo(valor) if SOLO_CODIGO else valor.strip()


# ---------------------------------------------------------------------------
# Plantilla de Albor y sus referencias
# ---------------------------------------------------------------------------

def _rutas_hojas(libro: zipfile.ZipFile) -> dict[str, str]:
    """Nombre de hoja -> ruta del XML dentro del xlsx."""
    workbook = libro.read("xl/workbook.xml").decode("utf-8-sig")
    rels = libro.read("xl/_rels/workbook.xml.rels").decode("utf-8-sig")
    destinos = {
        m.group("id"): m.group("dst")
        for m in re.finditer(r'<(?:\w+:)?Relationship\b(?=[^>]*\bId="(?P<id>[^"]+)")(?=[^>]*\bTarget="(?P<dst>[^"]+)")', rels)
    }
    rutas = {}
    for m in re.finditer(r'<(?:\w+:)?sheet\b(?=[^>]*\bname="(?P<n>[^"]+)")(?=[^>]*\br:id="(?P<id>[^"]+)")', workbook):
        destino = destinos.get(m.group("id"), "")
        destino = destino.lstrip("/")
        rutas[_desescapar(m.group("n"))] = destino if destino.startswith("xl/") else f"xl/{destino}"
    return rutas


def _desescapar(texto: str) -> str:
    return (texto.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&apos;", "'").replace("&amp;", "&"))


def es_plantilla(contenido: bytes) -> bool:
    """¿Es la plantilla de Albor? Mira solo los nombres de las hojas, así que
    sirve para reconocerla aunque la suelten en el recuadro equivocado."""
    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as z:
            return HOJA_DATOS in _rutas_hojas(z)
    except (zipfile.BadZipFile, KeyError):
        return False


# ---------------------------------------------------------------------------
# Listado de comprobantes de Albor (lo que ya está cargado)
# ---------------------------------------------------------------------------

def _html_de_listado(contenido: bytes) -> str | None:
    """El "Excel" que exporta la grilla de comprobantes de Albor es una tabla
    HTML en UTF-16 con extensión .xls. Devuelve el HTML, o None si no es eso."""
    for codificacion in ("utf-16", "utf-8-sig"):
        if codificacion == "utf-16" and not contenido.startswith((b"\xff\xfe", b"\xfe\xff")):
            continue
        try:
            texto = contenido.decode(codificacion)
        except UnicodeDecodeError:
            continue
        if "<table" in texto[:2000].lower():
            return texto
    return None


def es_listado_albor(contenido: bytes) -> bool:
    texto = _html_de_listado(contenido)
    return bool(texto) and "Nro CTG" in texto[:5000]


def ctg_en_listado(contenido: bytes) -> set[str]:
    """Los CTG de los comprobantes del listado (columnas Nro CTG y Carta Porte)."""
    import html as html_mod

    texto = _html_de_listado(contenido) or ""
    filas = [
        [html_mod.unescape(re.sub(r"<[^>]+>", "", celda)).replace("\xa0", " ").strip()
         for celda in re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", fila, re.S | re.I)]
        for fila in re.findall(r"<tr[^>]*>(.*?)</tr>", texto, re.S | re.I)
    ]
    if not filas:
        return set()
    columnas = [i for i, nombre in enumerate(filas[0]) if nombre in ("Nro CTG", "Carta Porte")]
    ctgs = set()
    for fila in filas[1:]:
        for i in columnas:
            valor = re.sub(r"\D", "", fila[i]) if i < len(fila) else ""
            if len(valor) == 11:
                ctgs.add(valor)
    return ctgs


def leer_plantilla(contenido: bytes) -> dict:
    """Valida la plantilla de Albor y devuelve sus referencias."""
    from openpyxl import load_workbook

    try:
        with zipfile.ZipFile(io.BytesIO(contenido)) as z:
            rutas = _rutas_hojas(z)
    except (zipfile.BadZipFile, KeyError) as e:
        raise ErrorArchivo(f"No es un .xlsx válido: {e}")
    if HOJA_DATOS not in rutas or HOJA_REFERENCIAS not in rutas:
        raise ErrorArchivo(
            f"No es la plantilla de importación de cosechas de Albor: le faltan las hojas "
            f"«{HOJA_DATOS}» y «{HOJA_REFERENCIAS}»."
        )
    libro = load_workbook(io.BytesIO(contenido), read_only=True)
    encabezado = [str(v or "").strip() for v in next(libro[HOJA_DATOS].iter_rows(max_row=1, values_only=True))]
    faltan = [c for c in COLUMNAS_USADAS if c not in encabezado]
    if faltan:
        raise ErrorArchivo("A la plantilla de Albor le faltan columnas: " + ", ".join(faltan))

    filas = libro[HOJA_REFERENCIAS].iter_rows(values_only=True)
    titulos = [str(v or "").strip() for v in next(filas)]
    indices = {clave: titulos.index(col) for clave, col in LISTAS.items() if col in titulos}
    listas: dict[str, list[str]] = {clave: [] for clave in LISTAS}
    for fila in filas:
        for clave, i in indices.items():
            if i < len(fila) and fila[i] not in (None, ""):
                listas[clave].append(str(fila[i]).strip())
    # Sin repetidos, en el orden de Albor.
    listas = {clave: list(dict.fromkeys(valores)) for clave, valores in listas.items()}
    return {"encabezado": encabezado, "listas": listas, "cargada": datetime.now().isoformat(timespec="seconds")}


def guardar_plantilla(contenido: bytes) -> dict:
    referencias = leer_plantilla(contenido)
    ALBOR_DIR.mkdir(parents=True, exist_ok=True)
    _escribir_atomico(PLANTILLA, contenido)
    _escribir_atomico(REFERENCIAS, json.dumps(referencias, ensure_ascii=False).encode())
    return referencias


def referencias() -> dict | None:
    if not (PLANTILLA.is_file() and REFERENCIAS.is_file()):
        return None
    return json.loads(REFERENCIAS.read_text(encoding="utf-8"))


def _escribir_atomico(destino: Path, contenido: bytes) -> None:
    temporal = destino.with_name(destino.name + ".tmp")
    temporal.write_bytes(contenido)
    temporal.replace(destino)


# ---------------------------------------------------------------------------
# Cultivos de Albor cargados a mano
# ---------------------------------------------------------------------------
# La plantilla de Albor exporta solo los cultivos de la campaña activa, así que
# para una campaña anterior no alcanza. Acá se carga la lista a mano (se copia
# del selector de cultivos de Albor) y se suma a la de la plantilla.

def cultivos_cargados() -> list[dict]:
    """Los cultivos cargados a mano. Cada uno: codigo, nombre y, si vino de la
    exportación de Albor, campo, lote, actividad y especie por separado."""
    if not CULTIVOS.is_file():
        return []
    datos = json.loads(CULTIVOS.read_text(encoding="utf-8"))
    # Formato viejo: una lista de textos "codigo - nombre".
    return [c if isinstance(c, dict) else {"codigo": codigo(c), "nombre": c.split(" - ", 1)[-1]}
            for c in datos]


def _guardar_cultivos(lista: list[dict]) -> list[dict]:
    unicos: dict[str, dict] = {}
    for c in lista:
        if c.get("codigo") and c.get("nombre"):
            unicos.setdefault(c["codigo"], c)
    ordenada = list(unicos.values())
    ALBOR_DIR.mkdir(parents=True, exist_ok=True)
    _escribir_atomico(CULTIVOS, json.dumps(ordenada, ensure_ascii=False, indent=1).encode())
    return ordenada


def guardar_cultivos(texto: str) -> list[dict]:
    """Una línea por cultivo: «02834 - El Bagual 5-6 SOJA ESP 25/26», o el
    código y el nombre separados por tabulación. Reemplaza lo anterior."""
    lista = []
    for linea in (texto or "").splitlines():
        linea = linea.strip()
        if not linea:
            continue
        if "\t" in linea:
            cod, _, nombre = linea.partition("\t")
        elif " - " in linea:
            cod, _, nombre = linea.partition(" - ")
        else:
            cod, _, nombre = linea.partition(" ")
        cod, nombre = cod.strip(), nombre.strip()
        if cod and nombre:
            lista.append({"codigo": cod, "nombre": nombre})
    return _guardar_cultivos(lista)


# Columnas de la exportación de cultivos de Albor que se usan.
COLUMNAS_CULTIVO = ("ID_Cultivo", "Nombre", "Campo", "Lote", "Actividad", "Especie", "IT_Dimension")


def es_export_cultivos(contenido: bytes) -> bool:
    try:
        encabezado = _encabezado_xlsx(contenido)
    except Exception:
        return False
    return "ID_Cultivo" in encabezado and "Campo" in encabezado


def _encabezado_xlsx(contenido: bytes) -> list[str]:
    from openpyxl import load_workbook

    hoja = load_workbook(io.BytesIO(contenido), read_only=True).worksheets[0]
    return [str(v or "").strip() for v in next(hoja.iter_rows(max_row=1, values_only=True))]


def importar_cultivos(contenido: bytes) -> list[dict]:
    """Lee la exportación de cultivos de Albor (menú Cultivos -> Excel).

    Trae campo, lote, actividad y especie en columnas separadas, así que el
    cruce con el ERP es exacto y no hace falta adivinar por el nombre. De paso
    viene el código de especie, que es el que pide la planilla.
    """
    from openpyxl import load_workbook

    hoja = load_workbook(io.BytesIO(contenido), read_only=True).worksheets[0]
    filas = hoja.iter_rows(values_only=True)
    titulos = [str(v or "").strip() for v in next(filas)]
    faltan = [c for c in ("ID_Cultivo", "Nombre", "Campo", "Lote") if c not in titulos]
    if faltan:
        raise ErrorArchivo(
            "No parece la exportación de cultivos de Albor: faltan las columnas "
            + ", ".join(faltan)
        )
    # "Actividad" y "Especie" aparecen dos veces: primero el código, después el
    # nombre. El nombre de la actividad es el que coincide con el ERP.
    def pos(nombre: str, cual: int = 0) -> int | None:
        apariciones = [i for i, t in enumerate(titulos) if t == nombre]
        return apariciones[cual] if len(apariciones) > cual else None

    i_id, i_nombre = pos("ID_Cultivo"), pos("Nombre")
    i_campo, i_lote = pos("Campo"), pos("Lote")
    i_actividad = pos("Actividad", 1)
    i_esp_cod, i_esp_nom = pos("Especie", 0), pos("Especie", 1)
    i_campana = pos("IT_Dimension")

    def dato(fila, i):
        return "" if i is None or i >= len(fila) or fila[i] is None else str(fila[i]).strip()

    lista = []
    for fila in filas:
        numero = dato(fila, i_id)
        nombre = dato(fila, i_nombre)
        if not numero or not nombre:
            continue
        lista.append({
            # En la plantilla el código va con ceros adelante: 2527 -> "02527".
            "codigo": numero.zfill(5) if numero.isdigit() else numero,
            "nombre": nombre,
            "campo": dato(fila, i_campo),
            "lote": dato(fila, i_lote),
            "actividad": dato(fila, i_actividad),
            "especie": dato(fila, i_esp_cod),
            "especie_nombre": dato(fila, i_esp_nom),
            "campana": dato(fila, i_campana),
        })
    if not lista:
        raise ErrorArchivo("La exportación de cultivos no tiene filas.")
    return _guardar_cultivos(lista)


def lista_cultivos(refs: dict | None) -> list[str]:
    """Las opciones de cultivo: primero las cargadas, después las de la plantilla."""
    cargados = cultivos_cargados()
    textos = [f"{c['codigo']} - {c['nombre']}" for c in cargados]
    vistos = {c["codigo"] for c in cargados}
    de_plantilla = (refs or {}).get("listas", {}).get("cultivo", [])
    return textos + [c for c in de_plantilla if codigo(c) not in vistos]


def especie_de_cultivo(codigo_cultivo: str) -> str:
    """El código de especie que Albor le puso al cultivo. Evita confirmar la
    especie aparte, y garantiza que no quede en contra del cultivo."""
    for c in cultivos_cargados():
        if c["codigo"] == codigo_cultivo and c.get("especie"):
            return c["especie"]
    return ""


# ---------------------------------------------------------------------------
# Equivalencias SoftCereal -> Albor
# ---------------------------------------------------------------------------

def equivalencias() -> dict[str, dict[str, str]]:
    datos = json.loads(EQUIVALENCIAS.read_text(encoding="utf-8")) if EQUIVALENCIAS.is_file() else {}
    return {tipo: dict(datos.get(tipo, {})) for tipo, *_ in TIPOS_EQUIVALENCIA}


def guardar_equivalencias(nuevas: dict[str, dict[str, str]], reemplazar: bool = False) -> None:
    """Suma (o con reemplazar, pisa) equivalencias. Un valor vacío la borra."""
    actuales = {t: {} for t, *_ in TIPOS_EQUIVALENCIA} if reemplazar else equivalencias()
    for tipo, valores in nuevas.items():
        for clave, valor in valores.items():
            if valor.strip():
                actuales.setdefault(tipo, {})[clave] = valor.strip()
            else:
                actuales.setdefault(tipo, {}).pop(clave, None)
    ALBOR_DIR.mkdir(parents=True, exist_ok=True)
    _escribir_atomico(EQUIVALENCIAS, json.dumps(actuales, ensure_ascii=False, indent=1, sort_keys=True).encode())


def _sin_cosecha(descripcion: str) -> str:
    """'Soja ESP Cosecha 25/26' -> 'Soja ESP'."""
    return re.sub(r"\s*cosecha\s*\d{2}/\d{2}\s*$", "", descripcion.strip(), flags=re.I).strip()


def _palabras_especie(texto: str) -> str:
    return re.sub(r"\besp\b", "especial", _normalizar(texto))


def _especie_exacta(descripcion: str, lista: list[str]) -> str:
    buscada = _palabras_especie(_sin_cosecha(descripcion))
    for opcion in lista:
        resto = opcion.split(" - ", 1)[-1]
        if _palabras_especie(resto) == buscada:
            return opcion
    return ""


def sugerir(tipo: str, clave: str, refs: dict) -> str:
    """La opción más parecida de Albor, o '' si no hay una razonable."""
    listas = refs["listas"]
    if tipo == "especie":
        return _especie_exacta(clave, listas["especie"])
    if tipo == "cultivo":
        exacto = _cultivo_exacto(clave)
        return exacto or _sugerir_cultivo(clave, lista_cultivos(refs))
    if tipo in ("deposito", "destino"):
        return _por_nombre(clave.split(" - ", 1)[-1], listas[tipo])
    return ""


def _por_nombre(descripcion: str, opciones: list[str]) -> str:
    """La opción de Albor cuyo nombre más se parece. Sirve para las plantas:
    'Planta de Silos Acceso' -> 'PSAA - Planta Silos Acceso Alberti',
    'Planta Baradero' -> 'P.BDRO - PLANTA BARADERO'."""
    buscado = _normalizar(descripcion).replace("silos", "silo")
    if not buscado:
        return ""
    palabras = set(buscado.split()) - {"planta", "de", "del", "la", "el"}
    mejor, puntaje = "", 0.0
    for opcion in opciones:
        nombre = _normalizar(opcion.split(" - ", 1)[-1]).replace("silos", "silo")
        if palabras and not palabras <= set(nombre.split()):
            continue
        parecido = difflib.SequenceMatcher(None, buscado, nombre).ratio()
        if parecido > puntaje:
            mejor, puntaje = opcion, parecido
    return mejor


def _numeros(texto: str) -> set[str]:
    return {n.lstrip("0") or "0" for n in re.findall(r"\d+", texto)}


def _cultivo_exacto(clave_cultivo: str) -> str:
    """Cruce exacto contra la exportación de cultivos de Albor, que trae campo,
    lote y actividad en columnas separadas. Es el camino bueno: sin adivinar."""
    partes = [x.strip() for x in clave_cultivo.split(" | ")]
    if len(partes) != 4:
        return ""
    campo, lote, especie, campana = partes
    buscado = (_normalizar(campo), _normalizar(lote), _normalizar(especie))
    candidatos = []
    for c in cultivos_cargados():
        if not c.get("campo"):
            continue
        if (_normalizar(c["campo"]), _normalizar(c.get("lote", "")),
                _normalizar(c.get("actividad", ""))) != buscado:
            continue
        texto = f"{c['codigo']} - {c['nombre']}"
        if c.get("campana") == campana:
            return texto          # misma campaña: no hay nada que dudar
        candidatos.append(texto)
    # De otra campaña no se sugiere: el código cambia en cada una.
    return ""


def _sugerir_cultivo(clave_cultivo: str, opciones: list[str]) -> str:
    """'El Bagual | 5-6 | Maiz ESP | 25/26' -> '02834 - El Bagual 5-6 MAIZ ESP 25/26'.

    En Albor el cultivo es un lote sembrado en una campaña, y su nombre junta
    campo, lote, especie y campaña. Se sugiere solo si coinciden el campo (al
    principio del nombre), la especie entera y los números del lote; si no, es
    preferible no sugerir nada, porque una sugerencia equivocada se confirma
    sin mirar. La campaña solo desempata: los códigos cambian de una a otra,
    pero la plantilla exporta una sola.
    """
    partes = clave_cultivo.split(" | ")
    if len(partes) != 4:
        return ""
    campo, lote, especie, campana = (x.strip() for x in partes)
    campo_n = _normalizar(campo)
    especie_n = set(_normalizar(especie).split())
    if not campo_n or not especie_n:
        return ""

    mejor, puntaje, candidatos = "", -1.0, []
    for opcion in opciones:
        descripcion = opcion.split(" - ", 1)[-1].strip()
        m = re.search(r"\s*(\d{2}/\d{2})$", descripcion)
        misma_campana = bool(m) and m.group(1) == campana
        if m:
            descripcion = descripcion[: m.start()]
        normalizada = _normalizar(descripcion)
        if not normalizada.startswith(campo_n):
            continue
        palabras = set(normalizada.split())
        # "SOJA" y "SOJA ESP" son especies distintas en Albor.
        if not especie_n <= palabras:
            continue
        resto = normalizada[len(campo_n):]
        if _numeros(lote) != _numeros(resto):
            continue
        # Las palabras del lote ("Entrada", "Fondo") también tienen que estar.
        del_lote = set(re.findall(r"[a-z]+", _normalizar(lote))) - {"lote"}
        if not del_lote <= set(resto.split()):
            continue
        candidatos.append(opcion)
        parecido = difflib.SequenceMatcher(
            None, _normalizar(f"{campo} {lote} {especie}"), normalizada).ratio()
        parecido += 1 if misma_campana else 0
        if parecido > puntaje:
            mejor, puntaje = opcion, parecido
    # Sin lote en el ERP (Batitú, La Nutria), solo si el campo tiene un cultivo.
    if not _normalizar(lote) and len(candidatos) != 1:
        return ""
    return mejor


def _cuit(valor: str) -> str:
    """'30712514112' y '30-71251411-2' -> '30-71251411-2' (como lo guarda Albor)."""
    digitos = re.sub(r"\D", "", valor or "")
    return f"{digitos[:2]}-{digitos[2:10]}-{digitos[10]}" if len(digitos) == 11 else ""


def _transportista(nombre: str, lista: list[str]) -> str:
    buscado = _normalizar(nombre)
    if not buscado:
        return ""
    exactos = [o for o in lista if _normalizar(o.split(" - ", 1)[-1]) == buscado]
    if exactos:
        return exactos[0]
    mejor = max(lista, key=lambda o: difflib.SequenceMatcher(None, buscado, _normalizar(o.split(" - ", 1)[-1])).ratio(), default="")
    if mejor and difflib.SequenceMatcher(None, buscado, _normalizar(mejor.split(" - ", 1)[-1])).ratio() >= 0.92:
        return mejor
    return ""


def _chofer(documento: str, lista: list[str]) -> str:
    """El DNI de SoftCereal dentro del CUIT de la lista de Albor:
    'OTTONELLO MARCOS (20-36272819-4)' -> '20-36272819-4'."""
    dni = re.sub(r"\D", "", documento)
    if not dni:
        return ""
    for opcion in lista:
        m = re.search(r"\((\d{2})-?(\d{7,8})-?(\d)\)\s*$", opcion)
        if m and m.group(2).lstrip("0") == dni.lstrip("0"):
            return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return ""


# ---------------------------------------------------------------------------
# Exportación de SoftCereal
# ---------------------------------------------------------------------------

# Campo interno -> encabezados posibles (normalizados) y cuál aparición tomar.
# La exportación repite "Descripción": la primera es el campo-lote, la segunda
# la especie y cosecha. "Sucursar Interna CPE" viene así, mal escrito, del ERP.
FUENTES = {
    "ctg": [("ctg", 0)],
    "ingreso": [("numero ingreso", 0), ("numero de ingreso", 0)],
    "ticket": [("numero ticket", 0), ("numero de ticket asignado", 0)],
    "planta": [("planta", 0)],
    "cosecha": [("cosecha", 0)],
    "lote": [("descripcion", 0)],
    "especie": [("descripcion", 1)],
    "fecha": [("fecha hora orden de carga", 0), ("fecha y hora calada", 0), ("fecha y hora peso bruto", 0)],
    "bruto": [("peso bruto reconocido", 0), ("peso bruto acopio", 0)],
    "tara": [("tara", 0)],
    "humedad": [("humedad", 0)],
    "estimado": [("kilos estimados", 0)],
    "km": [("kilometros acarreo", 0)],
    "transportista": [("nombre empresa de transporte", 0)],
    "chofer": [("carnet conductor transportista", 0)],
    "sucursal_cpe": [("sucursar interna cpe", 0), ("sucursal interna cpe", 0)],
    "numero_cpe": [("numero interno cpe", 0)],
    "observaciones": [("observaciones orden de carga", 0)],
}
OBLIGATORIAS = {"ctg": "CTG", "lote": "Descripción (campo-lote)", "especie": "Descripción (especie)",
                "cosecha": "Cosecha", "planta": "Planta", "bruto": "Peso Bruto Reconocido", "tara": "Tara"}


def _indices(encabezado: list[str]) -> dict[str, int]:
    apariciones: dict[str, list[int]] = {}
    for i, nombre in enumerate(encabezado):
        apariciones.setdefault(_normalizar(nombre), []).append(i)
    indices = {}
    for campo, candidatos in FUENTES.items():
        for nombre, n in candidatos:
            if len(apariciones.get(nombre, [])) > n:
                indices[campo] = apariciones[nombre][n]
                break
    faltan = [titulo for campo, titulo in OBLIGATORIAS.items() if campo not in indices]
    if faltan:
        raise ErrorArchivo(
            "El archivo no parece la exportación de ingresos de SoftCereal: falta la columna "
            + ", ".join(faltan)
        )
    return indices


def leer_softcereal(filas_planilla: list[list[str]]) -> list[dict]:
    """Filas de SoftCereal (encabezado primero) -> un dict por ingreso."""
    if len(filas_planilla) < 2:
        raise ErrorArchivo("El archivo no tiene filas de datos")
    indices = _indices(filas_planilla[0])
    ingresos = []
    # +2: los títulos de la exportación se descartan, así que se numera desde
    # el encabezado; sirve igual para ubicar el ingreso por número.
    for numero, datos in enumerate(filas_planilla[1:], start=2):
        fila = {campo: (datos[i].strip() if i < len(datos) else "") for campo, i in indices.items()}
        fila["fila"] = numero
        fila["origen"] = "softcereal"
        # "El Bagual-lote 5-6" -> campo "El Bagual", lote "5-6": la misma forma
        # que trae Oracle, así la tabla de equivalencias es una sola.
        campo, _, lote = (fila.get("lote") or "").partition("-")
        fila["campo"] = campo.strip()
        fila["lote"] = re.sub(r"^lote\s*", "", lote.strip(), flags=re.I)
        fila["especie"] = _sin_cosecha(fila.get("especie", ""))
        fila["planta_codigo"] = fila.get("planta", "")
        fila["chofer_dni"] = fila.get("chofer", "")
        ingresos.append(fila)
    return ingresos


def leer_oracle(filas: list[dict]) -> list[dict]:
    """Filas de oracle.buscar() -> la misma forma que leer_softcereal().

    Los datos ya vienen con su tipo (fechas, números), así que se pasan a texto
    una sola vez acá y el resto de la conversión no se entera de dónde salieron.
    """
    def texto(valor) -> str:
        if valor is None:
            return ""
        if isinstance(valor, datetime):
            return valor.strftime("%Y-%m-%d %H:%M:%S")
        if isinstance(valor, float) and valor.is_integer():
            return str(int(valor))
        return str(valor).strip()

    ingresos = []
    for numero, fila in enumerate(filas, start=1):
        ingresos.append({
            "fila": numero,
            "origen": "oracle",
            "esquema": texto(fila.get("esquema")),
            "sociedad": texto(fila.get("sociedad")),
            "ctg": texto(fila.get("ctg")),
            "punto_ingreso": texto(fila.get("punto_ingreso")),
            "numero_ingreso": texto(fila.get("numero_ingreso")),
            "ingreso": f"{texto(fila.get('punto_ingreso'))}-{texto(fila.get('numero_ingreso'))}",
            "cosecha": texto(fila.get("cosecha")),
            "especie": texto(fila.get("especie")) or texto(fila.get("especie_codigo")),
            "campo": texto(fila.get("campo")),
            "lote": texto(fila.get("lote")),
            "planta_codigo": texto(fila.get("planta_codigo")),
            "planta": texto(fila.get("planta")),
            "fecha": texto(fila.get("fecha_carga")) or texto(fila.get("fecha_hora")),
            "bruto": texto(fila.get("bruto")),
            "tara": texto(fila.get("tara")),
            "humedad": texto(fila.get("humedad")),
            "estimado": texto(fila.get("kilos_estimados")),
            "km": texto(fila.get("kilometros")),
            "transportista": texto(fila.get("transportista")),
            "cuit_transportista": texto(fila.get("cuit_transportista")),
            "chofer": texto(fila.get("chofer")),
            "cuit_chofer": texto(fila.get("cuit_chofer")),
            "sucursal_cpe": texto(fila.get("sucursal_cpe")),
            "numero_cpe": texto(fila.get("numero_cpe")),
            "observaciones": texto(fila.get("observaciones")),
        })
    return ingresos


# ---------------------------------------------------------------------------
# Número de ticket
# ---------------------------------------------------------------------------

def _clave_ticket(ingreso: dict) -> str:
    return "|".join((
        ingreso.get("esquema", ""),
        ingreso.get("punto_ingreso", ""),
        ingreso.get("numero_ingreso", "") or ingreso.get("ingreso", ""),
    ))


def parsear_ctgs(texto: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Texto pegado -> [(ctg, ticket)], y las líneas que no se entendieron.

    Una línea por CPE, con el CTG y a continuación el número de ticket de
    Albor. Separados por espacio, tabulación, coma o punto y coma, así se
    puede pegar directamente desde Excel:

        10134580361   31009
        10134826591,  31010
    """
    pares: list[tuple[str, str]] = []
    sueltas: list[str] = []
    for linea in (texto or "").splitlines():
        if not linea.strip():
            continue
        numeros = re.findall(r"\d+", linea)
        ctg = next((n for n in numeros if len(n) == 11), "")
        if not ctg:
            sueltas.append(linea.strip())
            continue
        resto = numeros[numeros.index(ctg) + 1:]
        pares.append((ctg, resto[0] if resto else ""))
    # Un CTG repetido va una sola vez; gana el primero, que es el que se ve.
    unicos: dict[str, str] = {}
    for ctg, ticket in pares:
        unicos.setdefault(ctg, ticket)
    return list(unicos.items()), sueltas


def asignar_tickets(ingresos: list[dict]) -> None:
    """Completa el número de ticket de los ingresos que no lo traen.

    Lo normal es que lo escriba el usuario junto con el CTG. Para los que
    queden sin número se usa uno correlativo arriba de TICKET_DESDE, guardado
    por ingreso: si la misma tanda se vuelve a convertir sale el mismo número
    y no se duplica nada en Albor.
    """
    pendientes_ = [i for i in ingresos if not (i.get("ticket") or "").strip()]
    if not pendientes_:
        return
    mapa = json.loads(TICKETS.read_text(encoding="utf-8")) if TICKETS.is_file() else {}
    usados = [int(v) for v in mapa.values() if str(v).isdigit()]
    siguiente = max(usados + [TICKET_DESDE]) + 1
    nuevos = False
    for ingreso in pendientes_:
        clave_t = _clave_ticket(ingreso)
        if clave_t not in mapa:
            mapa[clave_t] = str(siguiente)
            siguiente += 1
            nuevos = True
        ingreso["ticket"] = mapa[clave_t]
    if nuevos:
        ALBOR_DIR.mkdir(parents=True, exist_ok=True)
        _escribir_atomico(TICKETS, json.dumps(mapa, ensure_ascii=False, indent=1, sort_keys=True).encode())


# ---------------------------------------------------------------------------
# Conversión
# ---------------------------------------------------------------------------

def _campana(cosecha: str) -> str:
    """'2526' -> '25/26'."""
    digitos = re.sub(r"\D", "", cosecha)
    return f"{digitos[:2]}/{digitos[2:]}" if len(digitos) == 4 else cosecha


def clave(tipo: str, ingreso: dict) -> str:
    """El texto que identifica lo que hay que traducir, y que se le muestra al
    usuario en la pantalla de equivalencias."""
    especie = _sin_cosecha(ingreso.get("especie", ""))
    if tipo == "especie":
        return especie
    if tipo == "cultivo":
        campo = (ingreso.get("campo") or "").strip()
        lote = (ingreso.get("lote") or "").strip()
        return f"{campo} | {lote} | {especie} | {_campana(ingreso.get('cosecha', ''))}"
    # deposito y destino: el código de planta manda, la descripción es para leerlo
    codigo_planta = ingreso.get("planta_codigo") or ingreso.get("planta", "")
    descripcion = (ingreso.get("planta") or "").strip()
    nombre = f"Planta {codigo_planta}"
    return f"{nombre} - {descripcion}" if descripcion and descripcion != nombre else nombre


def pendientes(ingresos: list[dict], refs: dict) -> dict[str, list[dict]]:
    """Las equivalencias que faltan confirmar, con su sugerencia.
    Las especies con nombre idéntico en Albor se guardan solas."""
    guardadas = equivalencias()
    automaticas: dict[str, dict[str, str]] = {}
    faltan: dict[str, list[dict]] = {}
    for tipo, *_ in TIPOS_EQUIVALENCIA:
        conteo: dict[str, int] = {}
        for ingreso in ingresos:
            k = clave(tipo, ingreso)
            if k and k not in guardadas[tipo]:
                conteo[k] = conteo.get(k, 0) + 1
        for k, n in sorted(conteo.items()):
            sugerencia = sugerir(tipo, k, refs)
            if tipo == "especie" and sugerencia:
                automaticas.setdefault(tipo, {})[k] = sugerencia
            else:
                faltan.setdefault(tipo, []).append({"clave": k, "sugerencia": sugerencia, "filas": n})
    if automaticas:
        guardar_equivalencias(automaticas)
    return faltan


@dataclass
class Fila:
    numero: int
    ingreso: str
    valores: dict = field(default_factory=dict)
    errores: list[str] = field(default_factory=list)
    avisos: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errores


def _numero(texto: str) -> Decimal | None:
    try:
        return Decimal((texto or "").strip().replace(",", ".")) if (texto or "").strip() else None
    except InvalidOperation:
        return None


def _texto_numero(valor: Decimal | None, decimales: int = 0) -> str:
    if valor is None:
        return ""
    if decimales == 0:
        return str(int(valor.to_integral_value()))
    texto = f"{valor:.{decimales}f}".rstrip("0").rstrip(".")
    return texto.replace(".", SEPARADOR_DECIMAL)


def _fecha(texto: str) -> str:
    for formato in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(texto.strip(), formato).strftime(FORMATO_FECHA)
        except ValueError:
            continue
    return ""


def _armar(ingreso: dict, refs: dict, equiv: dict) -> Fila:
    listas = refs["listas"]
    fila = Fila(numero=ingreso["fila"], ingreso=ingreso.get("ingreso", ""))
    v = fila.valores

    def equivalente(tipo: str, obligatorio: bool, titulo: str) -> str:
        valor = equiv[tipo].get(clave(tipo, ingreso), "")
        if not valor and obligatorio:
            fila.errores.append(f"Falta la equivalencia de {titulo}: {clave(tipo, ingreso)}")
        return _valor_lista(valor) if valor else ""

    ctg = re.sub(r"\D", "", ingreso["ctg"])
    if len(ctg) != 11:
        fila.errores.append(f"CTG inválido: «{ingreso['ctg']}»")
    bruto, tara = _numero(ingreso["bruto"]), _numero(ingreso["tara"])
    if bruto is None or tara is None:
        fila.errores.append("Falta el peso bruto o la tara")
    elif tara >= bruto:
        fila.errores.append(f"La tara ({tara}) no es menor que el bruto ({bruto})")
    fecha = _fecha(ingreso.get("fecha", ""))
    if not fecha:
        fila.errores.append("Falta la fecha de carga")

    campana = _campana(ingreso["cosecha"])

    # El CUIT del chofer sale del ERP cuando se consulta Oracle; desde el Excel
    # de SoftCereal solo viene el DNI y hay que buscarlo en la lista de Albor.
    chofer = _cuit(ingreso.get("cuit_chofer", "")) or _chofer(
        ingreso.get("chofer_dni") or ingreso.get("chofer", ""), listas["chofer"])
    transportista = _transportista(ingreso.get("transportista", ""), listas["transportista"])
    if ingreso.get("transportista") and not transportista:
        fila.avisos.append(f"Transportista sin código en Albor: {ingreso['transportista']}")
    if ingreso.get("chofer") and not chofer:
        fila.avisos.append(f"Chofer sin CUIT en Albor: {ingreso['chofer']}")

    observaciones = " · ".join(p for p in (
        f"{ingreso.get('sociedad') or 'SoftCereal'} ingreso {ingreso['ingreso']}" if ingreso.get("ingreso") else "",
        ingreso.get("observaciones", ""),
    ) if p)[:250]

    # Si el cultivo salió de la exportación de Albor, su especie viene con él:
    # así no puede quedar una especie que contradiga al cultivo.
    cultivo = equivalente("cultivo", True, "cultivo")
    especie_cultivo = especie_de_cultivo(codigo(cultivo)) if cultivo else ""

    v.update({
        "Fecha": fecha,
        "Número de ticket": (ingreso.get("ticket") or "").replace(" ", "-"),
        "Código campaña": campana,
        "Código especie": especie_cultivo or equivalente("especie", not especie_cultivo, "especie"),
        # Albor tiene un solo tipo de grano ("UNICO"): si es así, va ese.
        "Tipo de Grano": _valor_lista(listas["grano"][0]) if len(listas["grano"]) == 1 else "",
        "Código cultivo": cultivo,
        "Código depósito destino": equivalente("deposito", True, "depósito destino"),
        "Código destino": equivalente("destino", False, "destino"),
        # Origen = destino, como lo carga hoy Albor: el productor no pesa en el campo.
        "Peso Origen Bruto": _texto_numero(bruto),
        "Peso Origen Tara": _texto_numero(tara),
        "Peso Origen Neto": _texto_numero(bruto - tara) if bruto is not None and tara is not None else "",
        "% Humedad Destino": _texto_numero(_numero(ingreso.get("humedad", "")), 2),
        "Peso Destino Bruto": _texto_numero(bruto),
        "Peso Destino Tara": _texto_numero(tara),
        "Peso Destino Neto": _texto_numero(bruto - tara) if bruto is not None and tara is not None else "",
        "Tipo de Flete": TIPO_FLETE,
        "Chofer (CUIT)": chofer,
        "Código transportista": _valor_lista(transportista) if transportista else "",
        "Tipo CPE": "E",
        # En Albor la carta de porte de una CPE es el número de CTG.
        "Carta de Porte": ctg,
        "Flete Corto": "No",
        "CTG": ctg,
        "Fecha Partida": fecha,
        "Observaciones remitente": observaciones,
        # La CPE ya existe en ARCA: que Albor no pida otra.
        "Obtener COT": "No",
        "Obtener CTG": "No",
    })
    return fila


# Columnas de Albor que llena la conversión (las demás van vacías).
COLUMNAS_USADAS = [
    "Fecha", "Número de ticket", "Código campaña", "Código especie", "Tipo de Grano",
    "Código cultivo", "Código depósito destino", "Código destino",
    "Peso Origen Bruto", "Peso Origen Tara", "Peso Origen Neto",
    "% Humedad Destino", "Peso Destino Bruto", "Peso Destino Tara", "Peso Destino Neto",
    "Tipo de Flete", "Chofer (CUIT)", "Código transportista", "Tipo CPE",
    "Carta de Porte", "Flete Corto", "CTG", "Fecha Partida",
    "Observaciones remitente", "Obtener COT", "Obtener CTG",
]


@dataclass
class Resultado:
    filas: list[Fila]
    repetidos: list[str]
    ya_en_albor: list[dict] = field(default_factory=list)

    @property
    def validas(self) -> list[Fila]:
        return [f for f in self.filas if f.ok]

    @property
    def con_error(self) -> list[Fila]:
        return [f for f in self.filas if not f.ok]

    @property
    def avisos(self) -> dict[str, int]:
        """Aviso -> cantidad de filas, para mostrarlos agrupados."""
        conteo: dict[str, int] = {}
        for f in self.validas:
            for a in f.avisos:
                conteo[a] = conteo.get(a, 0) + 1
        return dict(sorted(conteo.items(), key=lambda x: -x[1]))


def convertir(ingresos: list[dict], refs: dict, ya_en_albor: list[dict] | None = None) -> Resultado:
    equiv = equivalencias()
    asignar_tickets(ingresos)
    filas, repetidos, vistos = [], [], set()
    for ingreso in ingresos:
        fila = _armar(ingreso, refs, equiv)
        ctg = fila.valores["CTG"]
        if fila.ok and ctg in vistos:
            repetidos.append(f"CTG {ctg} (fila {fila.numero}): repetido, va una sola vez")
            continue
        vistos.add(ctg)
        filas.append(fila)
    return Resultado(filas, repetidos, ya_en_albor or [])


def separar_cargados(ingresos: list[dict], ctgs: set[str]) -> tuple[list[dict], list[dict]]:
    """(los que faltan cargar, los que ya están en Albor)."""
    faltan, cargados = [], []
    for ingreso in ingresos:
        (cargados if re.sub(r"\D", "", ingreso["ctg"]) in ctgs else faltan).append(ingreso)
    return faltan, cargados


# ---------------------------------------------------------------------------
# Tandas: las filas leídas, mientras se confirman las equivalencias
# ---------------------------------------------------------------------------

def guardar_tanda(ingresos: list[dict], origen: str, ya_en_albor: list[dict] | None = None,
                  faltantes: list[str] | None = None) -> str:
    TANDAS.mkdir(parents=True, exist_ok=True)
    limite = datetime.now() - RETENCION_TANDAS
    for vieja in TANDAS.glob("*.json"):
        if datetime.fromtimestamp(vieja.stat().st_mtime) < limite:
            vieja.unlink(missing_ok=True)
    tanda = uuid.uuid4().hex
    (TANDAS / f"{tanda}.json").write_text(json.dumps({"origen": origen, "ingresos": ingresos,
                    "ya_en_albor": ya_en_albor or [], "faltantes": faltantes or []},
                   ensure_ascii=False))
    return tanda


def leer_tanda(tanda: str) -> dict | None:
    if not re.fullmatch(r"[0-9a-f]{32}", tanda or ""):
        return None
    ruta = TANDAS / f"{tanda}.json"
    return json.loads(ruta.read_text(encoding="utf-8")) if ruta.is_file() else None


# ---------------------------------------------------------------------------
# Escritura
# ---------------------------------------------------------------------------

def _columna(n: int) -> str:
    letras = ""
    while n:
        n, resto = divmod(n - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def _hoja_xml(encabezado: list[str], filas: list[Fila], estilo: str) -> bytes:
    """La hoja de datos, con todas las celdas como texto: así viene la plantilla."""
    s = f' s="{estilo}"' if estilo else ""

    def celda(ref: str, texto: str, con_estilo: bool) -> str:
        atributos = s if con_estilo else ""
        if not texto:
            return f'<x:c r="{ref}"{atributos} t="inlineStr" />'
        return f'<x:c r="{ref}"{atributos} t="inlineStr"><x:is><x:t xml:space="preserve">{escape(texto)}</x:t></x:is></x:c>'

    partes = ['<?xml version="1.0" encoding="utf-8"?>'
              '<x:worksheet xmlns:x="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><x:sheetData>']
    partes.append('<x:row r="1">' + "".join(
        celda(f"{_columna(i)}1", nombre, False) for i, nombre in enumerate(encabezado, start=1)) + "</x:row>")
    for n, fila in enumerate(filas, start=2):
        partes.append(f'<x:row r="{n}">' + "".join(
            celda(f"{_columna(i)}{n}", str(fila.valores.get(nombre, "") or ""), True)
            for i, nombre in enumerate(encabezado, start=1)) + "</x:row>")
    partes.append("</x:sheetData></x:worksheet>")
    return "".join(partes).encode("utf-8")


def escribir(filas: list[Fila], refs: dict, destino: Path) -> Path:
    """Copia la plantilla de Albor cambiando solo la hoja de datos: la hoja
    Referencias (30 MB de XML) pasa tal cual, sin abrirla."""
    with zipfile.ZipFile(PLANTILLA) as origen:
        ruta = _rutas_hojas(origen)[HOJA_DATOS]
        original = origen.read(ruta).decode("utf-8-sig")
        m = re.search(r'<(?:\w+:)?c r="[A-Z]+2"[^>]*\bs="(\d+)"', original)
        estilo = m.group(1) if m else ""
        destino.parent.mkdir(parents=True, exist_ok=True)
        temporal = destino.with_name(destino.name + ".tmp")
        with zipfile.ZipFile(temporal, "w", zipfile.ZIP_DEFLATED) as salida:
            for item in origen.infolist():
                datos = _hoja_xml(refs["encabezado"], filas, estilo) if item.filename == ruta else origen.read(item)
                salida.writestr(item, datos)
        temporal.replace(destino)
    return destino
