# Rivara — Informar CPE a Visec

App de la intranet para armar el XLSX de **Registración de Cartas de Porte** que
pide Visec. Se cargan los números de CTG, la app consulta cada CPE en el web
service de ARCA (`wscpe`) y completa la plantilla de Visec con lo que devuelve
ARCA: intervinientes, plantas, grano, campaña y pesos.

Puerto **1009** del host `admin` (1003 es el portal, 1004 Horas Extra, 1006
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

Sigue el [contrato de despliegue](https://github.com/JP-Rev/Rivara-Infraestructura/blob/main/docs/contrato-despliegue-apps.md)
de `Rivara-Infraestructura`: código en `/srv/informarcpe`, datos en
`/storage/informarcpe`, puerto **1009** del host y **80** dentro del
contenedor, imagen `rivara-informarcpe:local`.

No es ninguno de los dos moldes del §3: no es una SPA de nginx (molde A) ni
Node con SQLite (molde B), sino Python con FastAPI, porque la app tiene que
firmar el ticket de WSAA y hablar SOAP con ARCA. Lo que sí respeta es todo lo
que el contrato pide del host: puerto, nombre de imagen, `/srv` para el código,
bind mount a `/storage` y healthcheck con `127.0.0.1`.

### Qué guarda en /storage, y qué se respalda

No usa base de datos. En `/storage/informarcpe` quedan cuatro cosas:

| Ruta en el host | Qué es | ¿Respaldar? |
|---|---|---|
| `/storage/informarcpe/certs/` | certificado y clave privada de ARCA | **sí, es lo único insustituible** |
| `/storage/informarcpe/cache/respuestas/` | respuesta cruda de cada CPE consultada | sí: es el respaldo de lo que se informó |
| `/storage/informarcpe/salida/` | los XLSX generados | no: se regeneran consultando otra vez |
| `/storage/informarcpe/cache/ta_*.json` | tickets de acceso de WSAA, vencen en 12 h | no |

Como no hay SQLite, **no aplica** el `sqlite3 ".backup"` de
[`almacenamiento.md`](https://github.com/JP-Rev/Rivara-Infraestructura/blob/main/docs/almacenamiento.md):
un `rsync` común alcanza. Son archivos que se escriben una vez y no se vuelven
a tocar.

```bash
rsync -a --exclude 'salida/' --exclude 'cache/ta_*' /storage/informarcpe/ DESTINO/informarcpe/
```

El destino tiene que estar en otro disco, por lo mismo que dice
`almacenamiento.md`: uno que falla se lleva los datos y la copia.

**La clave privada queda dentro del backup.** Es necesario —sin ella hay que
pedir un certificado nuevo a ARCA— pero significa que el backup hay que tratarlo
con el mismo cuidado que la clave.

Para que `/storage` no crezca sin control, la app borra sola los XLSX de más de
30 días y las respuestas de más de 365 (`RETENCION_SALIDA_DIAS` y
`RETENCION_RESPUESTAS_DIAS` del `.env`; `0` desactiva la purga).

### Traer el código

El ciclo del §10 del contrato:

```bash
/srv/deploy-app.sh informarcpe https://github.com/JP-Rev/Rivara-Interfaz-InformarCPE.git
```

Clona en `/srv/informarcpe`, verifica que `/storage` esté montado, crea
`/storage/informarcpe`, construye y levanta. Para actualizar, el mismo comando
sin la URL.

La primera vez va a cortar pidiendo el `.env` —es lo que hace el script cuando
hay un `.env.example` y no un `.env`—, así que el orden real es: correrlo,
poner los certificados, completar el `.env` y volver a correrlo.

### Los certificados de ARCA

Van en `/storage/informarcpe/certs`, **no en `/srv`**: `/srv` se borra y se
reclona sin perder nada, y los certificados no se pueden perder.

```bash
sudo mkdir -p /storage/informarcpe/certs
```

Si no sabés dónde están los archivos en este servidor:

```bash
sudo find /home /srv /opt /root -name '*.crt' -o -name '*.key' -o -name '*.p12' 2>/dev/null
```

Y después, **con la ruta real** en lugar de `ORIGEN`:

```bash
sudo cp ORIGEN/rivara.crt ORIGEN/rivara.key /storage/informarcpe/certs/
sudo chmod 600 /storage/informarcpe/certs/rivara.key
```

Si están en un `.p12` (lo habitual si los bajaste del sitio de ARCA), hay que
extraerlos; la clave privada tiene que quedar en PEM y **sin contraseña**:

```bash
openssl pkcs12 -in rivara.p12 -clcerts -nokeys  -out rivara.crt
openssl pkcs12 -in rivara.p12 -nocerts -nodes   -out rivara.key
```

El certificado tiene que tener habilitado el servicio **wscpe** en ARCA
(Administrador de Relaciones de Clave Fiscal), y el CUIT tiene que haber
intervenido en la CPE: una CPE ajena ARCA no la devuelve. Si alguna planta está
a nombre de otra sociedad, se agrega su certificado en `ARCA_CERTIFICADOS` y la
app reintenta con cada uno.

### Permisos

El contenedor corre como **UID 10001** y escribe en `/storage/informarcpe`
(contrato §7):

```bash
sudo chown -R 10001:10001 /storage/informarcpe
sudo chmod 600 /storage/informarcpe/certs/*.key
```

### Configuración y arranque

```bash
cd /srv/informarcpe
sudo cp .env.example .env
sudo nano .env          # ARCA_CERTIFICADOS, JWT_SECRET y los mapeos
sudo chmod 600 .env
sudo docker compose up -d --build
```

Verificar:

```bash
docker compose ps                        # tiene que decir healthy
curl -s http://localhost:1009/salud
```

`/salud` devuelve **200** con `"problemas": []` cuando está todo, y **503** con
la lista de lo que falta si no. El healthcheck del contenedor usa esa misma
ruta, así que un `unhealthy` significa configuración incompleta, no que la app
esté caída.

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
| puerto | 1009 |

Y agregar la fila del puerto 1009 en el §1 del contrato, en
`Rivara-Infraestructura`, como pide el checklist de la skill `rivara-app`.

### nginx

Consultar 100 CTG tarda unos minutos y el `proxy_read_timeout` por defecto de
nginx es de 60 segundos. Hoy las apps se publican directo en su puerto, así que
esto solo aplica cuando se ponga el nginx con TLS adelante (§11 del contrato):

```nginx
location / {
    proxy_pass http://127.0.0.1:1009;
    proxy_read_timeout 600s;
}
```

Mientras no haya proxy, alcanza con tandas más chicas (`MAX_CTG`).

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

## Si ARCA mueve el endpoint

Pasó en 2026: el WSDL de producción que traen las bibliotecas
(`serviciosjava.afip.gob.ar/wscpe/services/soap?wsdl`) empezó a dar 404 porque
el servicio se mudó a `cpea-ws.arca.gob.ar`. Para no depender de una versión
nueva de la app, las dos direcciones se pueden pisar desde el `.env`:

```ini
ARCA_URL_WSCPE=https://cpea-ws.arca.gob.ar/wscpe/services/soap?wsdl
ARCA_URL_WSAA=https://wsaa.afip.gov.ar/ws/services/LoginCms
```

Para buscar la que responde, desde el servidor:

```bash
for u in "https://cpea-ws.arca.gob.ar/wscpe/services/soap?wsdl" \
         "https://serviciosjava.afip.gob.ar/wscpe/services/soap?wsdl" ; do
  echo "$(curl -sL -o /dev/null -w '%{http_code}' --max-time 15 "$u")  $u"
done
```

El `?wsdl` importa: sin él la dirección es el endpoint SOAP y no el documento
que la app necesita para saber cómo llamarlo. Si se olvida, la app lo agrega.

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

Y la prueba del §9 del contrato, igual que lo que va a hacer el servidor:

```bash
docker build -t rivara-informarcpe:local .
docker run --rm -p 8080:80 \
  -e AUTH_DESACTIVADA=true -e CACHE_DIR=/tmp/cache -e SALIDA_DIR=/tmp/salida \
  --sysctl net.ipv4.ip_unprivileged_port_start=0 \
  rivara-informarcpe:local
```

`http://localhost:8080/` tiene que cargar el formulario, y `/salud` responder
503 con la lista de lo que falta (sin certificados montados es lo esperado).
