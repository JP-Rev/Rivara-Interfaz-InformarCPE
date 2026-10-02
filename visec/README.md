# Generador XLSX Visec (Registración de Cartas de Porte)

Lee `INGRESOS_PLANTA` y completa `plantilla_visec.xlsx` (la plantilla que pide Visec).

## Uso

```bash
cd visec
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # completar CUIT_EMPRESA, mapeos y DB_URL

# Prueba con un extracto exportado a Excel (no necesita DB)
python generar_xls_visec.py --xls /ruta/28.xls

# Desde la base, por rango de fechas (hasta inclusive)
python generar_xls_visec.py --desde 2026-10-01 --hasta 2026-10-01
```

Salida en `visec/salida/`: el `.xlsx` para subir a Visec y un `*_avisos.txt`
con los datos que faltaron por fila.

La consulta está en `query_ingresos.sql`. Los datos que no están en
`INGRESOS_PLANTA` (RUCA origen, CUIT corredor, CUIT remitente productor) se
agregan con JOIN en esa consulta usando los alias `RUCA_ORIGEN`,
`CUIT_CORREDOR` y `CUIT_REMITENTE_PRODUCTOR`; el script los toma solos.
