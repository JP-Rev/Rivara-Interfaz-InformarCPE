# Generador XLSX Visec (Registración de Cartas de Porte)

Consulta `SYSADMIN_ELN.INGRESOS_PLANTAS` (Oracle, vía SQL*Plus, igual que
`cpe_bolsatech.py`) y completa `plantilla_visec.xlsx`, la plantilla de Visec.

## Instalación (en el servidor donde corre SQL*Plus)

```bat
cd visec
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

Completar `.env` (mismas credenciales Oracle que Bolsatech).

## Uso

```bat
python generar_xls_visec.py --desde 2026-10-01
python generar_xls_visec.py --desde 2026-10-01 --hasta 2026-10-07 --tipos E,C
```

Salida en `visec\salida\`: el `.xlsx` para subir a Visec y un `*_avisos.txt`
con los datos que faltaron por CTG. Revisar los avisos antes de subir.

`buscar_tablas.sql` lista en qué tablas del esquema están los CUIT/RUCA que
todavía no completa el script.
