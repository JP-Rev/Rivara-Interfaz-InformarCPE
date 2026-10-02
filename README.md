# Rivara — Informar CPE a Visec

App de la intranet para armar el XLSX de **Registración de Cartas de Porte** que
pide Visec. Se cargan los números de CTG, la app consulta cada CPE en el web
service de ARCA (`wscpe`) y completa la plantilla de Visec con lo que devuelve
ARCA: intervinientes, plantas, grano, campaña y pesos.

Puerto **1007** del host `admin` (1003 es el portal, 1004 Horas Extra, 1006
Finanzas Axion).

## Por qué consultar ARCA y no la base

La plantilla de Visec pide once CUIT distintos (titular, remitentes comerciales
de venta primaria y secundaria, corredores, destinatario, destino) más los
números de RUCA de la planta de origen y de destino. En el sistema propio eso
está como códigos internos repartidos en varias tablas; en ARCA está completo y
es **el mismo dato que se declaró en la CPE**, así que no hay forma de que Visec
y ARCA queden desfasados.

De la base sigue haciendo falta una sola cosa: **qué CTG informar**. Eso lo
resuelve `scripts/ctg_soja.py`.

## Las dos piezas

| | Dónde corre | Qué hace |
|---|---|---|
| `scripts/ctg_soja.py` | Servidor con Oracle y SQL*Plus | Lista los CTG de ingresos de soja (especie 38) de un rango de fechas y deja un CSV |
| `app/` (esta app) | Servidor Ubuntu, Docker | Toma esos CTG, consulta ARCA y genera el XLSX de Visec |

El CSV del script aporta además dos datos que **ARCA no tiene**: la fecha del
movimiento y el peso que entró a stock, que son de la balanza. Si en vez del CSV
se pega una lista pelada de CTG, esas dos columnas se completan con lo que haya
en ARCA y la app lo avisa.

## Paso 1 — los CTG, en el servidor de Oracle

El script no necesita instalar nada: usa solo la biblioteca estándar de
Python 3.8+. Se copia `scripts/ctg_soja.py` y `scripts/Generar CTG.bat` a una
carpeta cualquiera del equipo que tiene SQL*Plus —por ejemplo
`C:\Users\JREALE\Desktop\Visec Informa CPE`— y trabaja ahí: lee el `.env` de
esa carpeta y escribe el CSV en esa misma carpeta.

La primera corrida crea un `.env` con las claves vacías y corta avisando. Se
completa con las mismas credenciales que usa `cpe_bolsatech.py`:

```ini
SQLPLUS_EXE=D:\oracle\product\11.2.0\client_1\bin\sqlplus.exe
ORACLE_USUARIO=
ORACLE_PASSWORD=
ORACLE_ALIAS=BASE

ESQUEMAS=SYSADMIN:Rivara,SYSADMIN_ELN:La Tranquera Verde,SYSADMIN_PRA:Pradera Natural
```

### Un esquema por sociedad

Cada sociedad tiene su esquema en la misma base y con las mismas credenciales:

| Esquema | Sociedad |
|---|---|
| `SYSADMIN` | Rivara |
| `SYSADMIN_ELN` | La Tranquera Verde |
| `SYSADMIN_PRA` | Pradera Natural |

Por omisión consulta los tres y agrega al CSV las columnas `empresa` y
`esquema`, así se ve de quién es cada CTG. Con `--esquema` se consulta uno solo.
Los nombres salen de `ESQUEMAS` del `.env`, así que agregar una sociedad no
toca el código.

Un CTG que apareciera en dos esquemas se informa una sola vez, con un aviso en
la consola: Visec lo rechazaría por duplicado.

### Correrlo

Doble clic en **Generar CTG.bat** (pide las fechas y la empresa) o desde la
consola:

```bat
python ctg_soja.py                                      :: hoy, las tres sociedades
python ctg_soja.py --desde 2026-10-01
python ctg_soja.py --desde 2026-10-01 --hasta 2026-10-07
python ctg_soja.py --desde 2026-10-01 --esquema SYSADMIN_PRA
python ctg_soja.py --desde 2026-10-01 --planta 2
```

Deja un `ctg_soja_AAAAMMDD_AAAAMMDD.csv` con una fila por CTG. Ese archivo es el
que se sube en la app.

Filtra `IPL_ESPECIE = '38'` (soja). Con `--especie` se puede informar otro grano
sin tocar el código.

**Ese `.env` tiene la clave de la base.** Queda en el Escritorio del equipo, así
que no se comparte la carpeta ni se sube a ningún repositorio.

## Paso 2 — la app, en el servidor Ubuntu

### Certificados de ARCA

El certificado y la clave privada van en `./certs` **y no se suben a GitHub**
(`.gitignore` ya los excluye). El `docker-compose.yml` los monta en `/certs`
de solo lectura.

Si no sabés dónde están los archivos en este servidor:

```bash
sudo find /home /srv /opt /root -name '*.crt' -o -name '*.key' -o -name '*.p12' 2>/dev/null
```

Y después, **con la ruta real** en lugar de `ORIGEN`:

```bash
mkdir -p certs
cp ORIGEN/rivara.crt certs/
cp ORIGEN/rivara.key certs/
chmod 600 certs/rivara.key
```

La clave privada tiene que estar **sin contraseña** y en PEM. Si está en `.p12`:

```bash
openssl pkcs12 -in rivara.p12 -clcerts -nokeys -out certs/rivara.crt
openssl pkcs12 -in rivara.p12 -nocerts -nodes -out certs/rivara.key
```

El certificado tiene que tener habilitado el servicio **wscpe** en ARCA
(Administrador de Relaciones de Clave Fiscal), y el CUIT tiene que haber
intervenido en la CPE: una CPE ajena ARCA no la devuelve. Si alguna planta está
a nombre de otra sociedad, se agrega su certificado en `ARCA_CERTIFICADOS` y la
app reintenta con cada uno.

### Configuración y arranque

```bash
cp .env.example .env
# completar ARCA_CERTIFICADOS, JWT_SECRET y los mapeos
docker compose up -d --build
docker compose logs -f informarcpe
```

Verificar que levantó:

```bash
curl -s http://localhost:1007/salud
```

Devuelve el entorno y una lista `problemas` vacía cuando está todo configurado.
Si falta algo, lo dice ahí y también arriba en la pantalla de la app.

**Arrancar en `ARCA_ENTORNO=homologacion`.** Recién cuando una CPE real salga
bien se pasa a `produccion`.

### Registrarla en el portal

En el portal, Administrar → agregar app:

| Campo | Valor |
|---|---|
| id | `informarcpe` |
| nombre | Informar CPE a Visec |
| descripción | Carta de porte electrónica para Visec |
| icono | `camion` |
| puerto | 1007 |

### nginx

Consultar 100 CTG tarda unos minutos y el `proxy_read_timeout` por defecto de
nginx es de 60 segundos, así que si la app va detrás de un proxy hay que
subirlo:

```nginx
location / {
    proxy_pass http://127.0.0.1:1007;
    proxy_read_timeout 600s;
}
```

Si no, alcanza con tandas más chicas (`MAX_CTG`).

### Tipografía

El sistema de diseño pide que Archivo vaya empaquetada y nunca desde Google
Fonts, porque la LAN puede no tener salida a internet. Copiar el `.woff2`
variable a `app/static/fuentes/archivo-variable.woff2` (está en el
`node_modules/@fontsource-variable/archivo` del portal). Si el archivo no está,
el CSS cae a la tipografía del sistema y la app funciona igual, solo se ve
distinta.

## Qué columnas completa, y de dónde

| Columna de Visec | Origen |
|---|---|
| Fecha y Hora Movimiento | CSV del script (`fecha_movimiento`); si no, `cabecera.fechaInicioEstado` de ARCA |
| Fecha CPE | `cabecera.fechaEmision` |
| Número CPE | `cabecera.sucursal` + `cabecera.nroOrden` |
| Número CTG | `cabecera.nroCTG` |
| CUIT Titular | `cabecera.cuitSolicitante` |
| Número RUCA Origen | `origen.operador.planta` |
| CUIT Remitente Comercial Productor | `retiroProductor.cuitRemitenteComercialProductor` |
| CUIT Rte Comercial Venta Primaria / Secundaria / Secundaria 2 | `intervinientes.*` |
| CUIT Corredor Venta Primaria / Secundaria | `intervinientes.*` |
| CUIT Destinatario | `destinatario.cuit` |
| CUIT Destino | `destino.cuit` |
| Número RUCA Destino | `destino.planta` |
| Código Producto | `datosCarga.codGrano`, traducido con `MAPA_GRANO_VISEC` |
| Campaña | `datosCarga.cosecha` |
| Peso Neto Carga (Kg) | `datosCarga.pesoBruto` − `pesoTara` |
| Peso Ingreso Stock (Kg) | CSV del script (`peso_ingreso_stock`); si no, `pesoBrutoDescarga` − `pesoTaraDescarga` |
| Último Almacenamiento, Tipo Movimiento | valores fijos del `.env` |

Quedan **sin completar**, porque no están en la CPE automotor de ARCA:
**Número RENSPA**, **Número CTG Asignado** y los dos **pesos por UP**. Hay que
confirmar con Visec si son obligatorios para este tipo de movimiento.

También falta que Visec indique qué valores acepta en **Tipo Movimiento** y
**Último Almacenamiento**, y en qué formato quiere el número de CPE y las
fechas (`FORMATO_NUMERO_CPE`, `FORMATO_FECHA_HORA`, `FORMATO_FECHA`).

Con `ARCA_GUARDAR_RESPUESTAS=true` cada consulta deja la respuesta cruda en
`/data/cache/respuestas/cpe_<ctg>_<fecha>.json`. Es lo que hay que mirar para
cerrar el mapeo de esas columnas con una CPE real:

```bash
docker compose exec informarcpe ls /data/cache/respuestas
docker compose exec informarcpe cat /data/cache/respuestas/cpe_10235396826_*.json
```

## Qué hace la app con los datos

- **Solo consulta.** Usa `consultarCPEAutomotor` y nada más: no autoriza, no
  anula ni modifica CPE, así que no hay forma de alterar una carta de porte.
- El XLSX se genera sobre la plantilla original de Visec. Si Visec cambia las
  columnas, el proceso se detiene con el detalle en vez de generar un archivo
  que les va a rebotar.
- Todas las celdas se escriben como texto, igual que la plantilla: si fueran
  números, Excel se comería los ceros a la izquierda de los CUIT y los CTG.
- Antes de bajar el archivo se ve una tabla con el estado de cada CPE y los
  avisos por celda vacía.
- La sesión es la cookie `rivara_he` del portal, así que no hay un segundo
  login. En desarrollo, `AUTH_DESACTIVADA=true`.

## Desarrollo local

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # AUTH_DESACTIVADA=true, SALIDA_DIR=./data/salida, CACHE_DIR=./data/cache
uvicorn app.main:app --reload --port 8000
```
