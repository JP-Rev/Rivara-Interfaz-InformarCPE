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

## Versión operario y versión admin

El rol sale del portal: la cookie de sesión `rivara_he` trae `role` (`ADMIN` u
`OPERADOR`, según el "es admin" de cada usuario del portal). No hay una lista
de usuarios propia de esta app: los permisos se administran en el portal.

| | Operario | Admin |
|---|---|---|
| Quién | todo usuario del portal | los `ADMIN` del portal |
| Archivo que se sube | Excel del editor SQL del ERP | CSV de `ctg_soja.py` (o el Excel) |
| Descarga | la consulta SQL | el script con SQL\*Plus (`.zip` con `ctg_soja.py` y `Generar CTG.bat`) |

Un admin elige la versión al entrar, y la elección queda guardada en una cookie
de esta app (`informarcpe_modo`); se cambia con "Cambiar versión", arriba. El
control es del lado del servidor: un operario que pida `/modo/admin` o
`/script/ctg_soja.zip` recibe 403, aunque tenga la cookie de modo.

## Paso 1 — los CTG desde el editor SQL del ERP (operarios)

La forma de todos los días. La consulta está en
`app/consultas/ingresos_erp.sql` y se descarga desde la app (botón
"Descargar la consulta SQL"):

1. Copiarla en el editor SQL del ERP, entrando con la sociedad que corresponda.
2. Cambiar solo las dos fechas (DD/MM/AAAA; la de hasta es inclusive).
3. Ejecutarla y exportar el resultado a **Excel**.
4. Arrastrar el Excel en la app.

El Excel del ERP trae unos renglones de título y después la fila de
encabezados: la app arranca en la fila que tiene la columna `CTG` y reconoce
cada columna por el nombre, así que no importa el orden. También acepta el TXT
que exporta el editor (ancho fijo, sin encabezado), leyéndolo por posición:
eso solo funciona mientras la consulta no cambie, por eso conviene el Excel.

## Paso 1 (alternativa) — los CTG con el script, en el servidor de Oracle

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
| `/storage/informarcpe/certs/` | certificado y clave privada de ARCA, y `respaldo/` con los reemplazados | **sí, es lo único insustituible** |
| `/storage/informarcpe/cache/avisos_certificados.json` | qué avisos de vencimiento ya se mandaron | no |
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

### Vencimiento y renovación del certificado

La app revisa los certificados al arrancar y cada `CERT_CHEQUEO_HORAS`, y
manda un mail al entrar en cada umbral de `CERT_AVISO_DIAS` (por omisión 30,
15, 7, 3 y 1 días antes), una sola vez por umbral, a `SMTP_TO`. El mail trae
el link a la página **Certificados** (`APP_URL` + `/certificados`), que solo
ven los admins.

Ahí se renueva arrastrando el `.crt` nuevo que da ARCA, y el `.key` solo si se
pidió con una clave nueva. Antes de reemplazar se valida que sea un
certificado, que el CUIT sea el mismo, que no esté vencido y que corresponda a
la clave. Los anteriores quedan en `/storage/informarcpe/certs/respaldo`, así
que volver atrás es copiar de ahí. Por eso el directorio `certs` se monta con
escritura.

El correo usa los mismos datos que `cpe_bolsatech.py` (Office 365:
`smtp.office365.com`, puerto 587). El botón "Enviar mail de prueba" de la
página de certificados verifica la configuración. Si el login falla con
`5.7.139`, Microsoft tiene desactivado el SMTP autenticado para esa cuenta y se
habilita en el centro de administración de Microsoft 365.

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

## Importar CPE a Albor

Segunda sección del menú lateral, para todos los usuarios. Se pegan los CTG con
su número de ticket de Albor al lado, la app busca los ingresos a planta en el
**ERP (Oracle)** y arma la planilla de importación de comprobantes de cosecha de
Albor (`ImportacionCosecha.xlsx`, hoja **Cosechas a importar**).

Una línea por CPE, separando con espacio, tabulación o coma, así se puede pegar
directo desde Excel:

```
10134580361   31009
10134826591,  31010
```

Antes esto salía de una exportación manual de SoftCereal. Esa vía sigue
funcionando como respaldo: si no hay base configurada, la pantalla pide el
Excel en vez de los CTG.

### La conexión al ERP

La base es **Oracle 11.2**, así que `python-oracledb` tiene que correr en modo
"thick": el modo "thin" necesita 12.1 o posterior. Por eso la imagen trae el
**Oracle Instant Client 19** (ver `Dockerfile`), y por eso la imagen base está
fijada a `-bookworm`: en Debian 13 la librería `libaio.so.1` pasó a llamarse
`libaio.so.1t64` y el cliente no arranca.

En el `.env`:

```
ORACLE_DSN=192.168.0.20:1521/base          # host:puerto/service_name
ORACLE_USUARIO=
ORACLE_PASSWORD=
ORACLE_ESQUEMAS=SYSADMIN:Rivara,SYSADMIN_ELN:La Tranquera Verde,SYSADMIN_PRA:Pradera Natural
```

Las tres sociedades son esquemas de la misma base: una sola conexión las lee a
todas, y un CTG se busca en todas porque no se sabe de antemano en cuál está.
El nombre del esquema se interpola en el SQL, así que **solo puede venir del
`.env`**, nunca de la pantalla; los CTG van como bind variables.

**El usuario de Oracle debería tener permiso de `SELECT` solamente**, no ser el
dueño del esquema. La app nunca escribe en el ERP.

### La plantilla de Albor

Se baja de Albor y se sube desde la misma pantalla. Queda en
`/storage/informarcpe/albor/plantilla_cosecha.xlsx`. **No va en el repositorio**:
su hoja **Referencias** trae nombres y CUIT de personas. La app lee esas listas
una vez al cargarla (`referencias.json`) y en cada conversión copia la plantilla
cambiando solo la hoja de datos, así Referencias viaja intacta.

Ojo: la plantilla exporta **solo la campaña activa**. Si los ingresos son de una
campaña anterior, sus cultivos no van a estar en la lista y hay que conseguir la
plantilla de esa campaña.

### Equivalencias

Lo que el ERP y Albor llaman distinto se traduce con una tabla que se confirma
una sola vez y queda en `/storage/informarcpe/albor/equivalencias.json`:

| Tipo | Clave del ERP | Ejemplo en Albor |
|---|---|---|
| Especie | `Maiz ESP` | `1113 - MAIZ ESPECIAL` (se resuelve sola si el nombre coincide) |
| Cultivo | `El Bagual \| 15 \| Soja ESP \| 25/26` | `02836 - El Bagual LTV 15 SOJA ESP 25/26` |
| Depósito destino | `Planta 16 - Planta de Silos Acceso` | `PSAA - Planta Silos Acceso Alberti` |
| Destino (opcional) | `Planta 16 - Planta de Silos Acceso` | `9 - Planta Silo Acceso Acceso Alberti` |

**El campo no se traduce con una tabla.** El mismo campo del ERP puede ir a
campos distintos de Albor según el lote, así que no hay una equivalencia
estable: se elige el cultivo, que ya es por campo y lote.

Para proponerlo, primero se busca el cultivo de Albor que coincide en campo,
lote, actividad y campaña. Si no hay (porque en Albor ese lote quedó bajo otro
campo), se busca por lote, actividad y campaña **en cualquier campo**, y se
propone solo si queda uno: hay 17 campos con lote «Único» de soja, y con varios
candidatos elige el usuario. Como el nombre del cultivo incluye el campo, en la
pantalla se ve si cambió.

En las equivalencias opcionales, dejar el valor vacío significa «acá no va
nada» y se guarda así; si no, se volvería a preguntar en cada tanda.

Los códigos de especie **no coinciden** entre los dos sistemas: `05` es
`Maiz ESP` en el ERP y `CEBADA` en Albor. Por eso nunca se pasa el código del
ERP tal cual; siempre va por la tabla.

Si al convertir aparece algo sin equivalencia, la app la pide antes de armar la
planilla, con una sugerencia cuando hay una segura. Se revisan en
**Equivalencias** (`/albor/equivalencias`).

### Cultivos (`/albor/cultivos`)

La plantilla de importación trae **solo los cultivos de la campaña activa**, así
que para una campaña anterior no alcanza. En Albor, menú **Cultivos**, se filtra
por campaña, se exporta la grilla a Excel y se sube ahí.

Esa exportación es mucho mejor que la plantilla: trae `ID_Cultivo`, `Campo`,
`Lote`, `Actividad`, `Especie` e `IT_Dimension` (la campaña) **en columnas
separadas**. Con eso el cultivo se encuentra por cruce exacto de campo + lote +
actividad + campaña, sin adivinar por el nombre. Dos detalles:

- La columna que coincide con la especie del ERP (`Maiz ESP`) es **Actividad**,
  no `Especie`, que trae el nombre de Albor (`MAIZ ESPECIAL`).
- El `ID_Cultivo` va sin ceros (`2527`) y la planilla los pide (`02527`).

Como la exportación trae el código de especie de cada cultivo, **la especie no
hay que confirmarla**: sale del cultivo elegido, y así no puede quedar una
especie que contradiga al cultivo.

Debajo, la misma pantalla tiene la lista **editable fila por fila** (código,
campo, lote, actividad, campaña y código de especie), con un buscador. Sirve
para agregar un cultivo nuevo o corregir uno sin volver a exportar de Albor.
Guardar reemplaza la lista entera, así que para borrar un cultivo se le vacía el
código; el buscador solo esconde filas, no las saca del formulario. Todo queda
en `/storage/informarcpe/albor/cultivos.json`.

**Los campos y lotes del ERP no hace falta cargarlos**: salen de la consulta a
Oracle en cada tanda, así que siempre están al día.

### Número de ticket

Lo escribe el usuario al lado del CTG. Para los que queden sin número, la app usa
uno correlativo desde `ALBOR_TICKET_DESDE` (1.000.000 por omisión), guardado por
ingreso en `tickets.json`: si la misma tanda se convierte de nuevo sale el mismo
ticket y no se duplica nada.

### Columnas

| Albor | Sale del ERP |
|---|---|
| Fecha, Fecha Partida | `IPL_FECHA_HORA_CARGA` |
| Número de ticket | el que se escribe al lado del CTG |
| Código campaña | `IPL_COSECHA` (`2526` → `25/26`) |
| Código cultivo | cruce exacto con los cultivos cargados, o equivalencia |
| Código especie | la del cultivo elegido; si no se conoce, equivalencia |
| Tipo de Grano | el único de Albor (`00001`) |
| Código depósito destino, Código destino | equivalencia por planta |
| Peso Origen y Destino Bruto / Tara / Neto | `IPL_PESO_BRUTO_RECONOCIDO`, `IPL_TARA` (origen = destino) |
| % Humedad Destino | `IPL_HUMEDAD` |
| Código transportista | `IPL_NOMBRE_EMPRESA_TRANSPORTE`, buscado en la lista de Albor |
| Chofer (CUIT) | `IPL_CAMIONERO` cruzado con `ENTIDADES` |
| Tipo CPE / Carta de Porte / CTG | `E` / CTG / CTG |
| Tipo de Flete, Flete Corto, Obtener CTG, Obtener COT | `T`, `No`, `No`, `No` |
| Observaciones remitente | sociedad, número de ingreso y `IPL_OBSERVACIONES_CARGA` |
| Código tipo de comprobante, Código socio y el resto | vacío |

Una fila sin CTG válido, sin fecha, con tara mayor o igual al bruto o sin
especie, cultivo o depósito queda afuera y se muestra. Un transportista o chofer
que no está en Albor no frena la fila: va vacío y se avisa.

### Lo que ya está en Albor

Si se sube además el listado de comprobantes de acopio (`AlborExcel.xls`, que
es una tabla HTML en UTF-16 con extensión `.xls`), los CTG que ya figuran ahí
quedan afuera de la planilla para no cargarlos dos veces.

Formato de los valores, ajustable en el `.env` si Albor los rechaza:

| Variable | Default | Qué cambia |
|---|---|---|
| `ALBOR_SOLO_CODIGO` | `1` | `1`: `02836`; `0`: `02836 - El Bagual LTV 15 ...` |
| `ALBOR_SEPARADOR_DECIMAL` | `,` | humedad `12,8` o `12.8` |
| `ALBOR_FORMATO_FECHA` | `%d/%m/%Y` | fechas |
| `ALBOR_TIPO_FLETE` | `T` | `T` Tercero, `I` Interno |
| `ALBOR_TICKET_DESDE` | `1000000` | desde dónde numera los tickets |

## Informar CPE a BolsaTech

Tercera sección del menú. Se pegan los CTG de soja, la app busca los ingresos
en el ERP y los informa a **BolsaTech (SIBIO)** para el muestreo de
biotecnologías. Reemplaza a los scripts `cpe_bolsatech.py` y
`cpe_bolsatech_desde_csv.py`.

### Esta sección escribe afuera

Es la única parte de la app que no se limita a generar un archivo: un alta
registrada en BolsaTech **no se deshace desde acá**. Por eso el flujo es en dos
pasos y dos botones distintos:

1. **Buscar en el ERP**: consulta y muestra qué se informaría, con los datos ya
   armados. No envía nada.
2. **Informar a BolsaTech**: envía. Hay que tildar una casilla de confirmación.

Los scripts usaban `MODO_PRUEBA` en el `.env` para esto. La pantalla de revisión
cumple la misma función y no depende de que nadie se olvide de cambiar una
variable.

De cada CPE queda guardado qué pasó en
`/storage/informarcpe/bolsatech/estado.json` (lo que hacía `cpe_control.csv`),
así una tanda repetida no vuelve a informar lo que ya estaba. Los XML enviados y
recibidos quedan en `bolsatech/xml/`: es lo único que hay si después hay que
reclamar algo.

### Las tres operaciones

| Operación | Para qué | SOAP | URL |
|---|---|---|---|
| `consultarCartasDePorte` | ¿ya está informada? | 1.2 | `BOLSATECH_URL` |
| `registrarCartasDePorte` | el alta, devuelve `trackId` | 1.2 | `BOLSATECH_URL` |
| `confirmarRecepcionDeTrackId` | confirma ese `trackId` | 1.1 | `BOLSATECH_TRACK_URL` |

La autenticación va en el header, con WS-Security `UsernameToken` y la
contraseña en texto plano: es lo que pide el servicio.

**Sin `BOLSATECH_TRACK_URL`** el alta sale igual pero queda como «sin
confirmar»; es lo que en el script aparecía como
`REGISTRADA_PENDIENTE_CONFIRMACION`.

### Las dos cuentas

Hay credenciales de Rivara y de MLiaudat. Cuál se usa lo decide el **prefijo del
identificador de muestra**: si empieza con `779` (`BOLSATECH_PREFIJO_MLIAUDAT`)
va por MLiaudat, si no por Rivara.

### El identificador de muestra

Sale de `INFORMACION_BIOTECNOLOGIAS`, cruzando por CTG. Cuando no está cargado
se arma con un prefijo más el CTG, y el prefijo se elige en la pantalla:

- **Según la planta de cada ingreso** (lo normal): cada uno usa el prefijo de su
  planta, así una tanda que mezcla Baradero y Alberti no se equivoca.
- **Baradero (7790)** o **Alberti (5140)**: para forzarlo a mano.

Ojo que el **puerto** de Alberti es `5150` y el **prefijo del id de muestra** es
`5140`. Son distintos y es fácil confundirlos.

### Pendientes de confirmar (`/bolsatech/pendientes`)

Un alta que salió bien pero cuyo `trackId` nunca se confirmó queda como «sin
confirmar». El `trackId` sigue valiendo, así que se puede confirmar después,
una vez configurada `BOLSATECH_TRACK_URL`.

Esa pantalla también **importa el `cpe_control.csv`** del script viejo (está en
su carpeta `data/`). De ahí salen los `trackId` que quedaron pendientes y, de
paso, la app se entera de qué CPE ya están informadas para no repetirlas. Los
estados se traducen así:

| En el CSV | En la app |
|---|---|
| `REGISTRADA_Y_CONFIRMADA` | informada |
| `REGISTRADA_PENDIENTE_CONFIRMACION` | sin confirmar |
| `YA_EXISTE` | ya estaba |
| `ERROR`, `ERROR_REGISTRO` | error |

Lo que en la app ya quedó resuelto no se pisa con el CSV: manda lo más nuevo.

### La corrida por fechas (lo que hacía la tarea programada)

`cpe_bolsatech.py` corría con el Programador de tareas de Windows: buscaba en
el ERP los ingresos de soja de un rango de fechas y los informaba, sin que
nadie cargara CTG. Eso está en el botón **Correr ahora** de `/bolsatech` y en
una tarea que la app corre sola.

**No hace falta `cron` ni el Programador de tareas**: el ciclo vive dentro de la
app, como el que avisa los vencimientos de certificados. Un proceso menos que
puede quedar apagado sin que nadie se entere.

Viene **apagada**, a propósito: es la única parte de la app que escribe en un
sistema externo sin que nadie mire. Para encenderla, en el `.env`:

```ini
BOLSATECH_AUTOMATICO=true
BOLSATECH_CADA_HORAS=6
BOLSATECH_FECHA_INICIAL=2026-01-01
BOLSATECH_SOLAPAMIENTO_MINUTOS=5
BOLSATECH_LIMITE=200
```

Y reiniciar el contenedor (`docker compose up -d --force-recreate`). Al
terminar cada corrida manda el resumen por mail, igual que el script.

**La primera corrida sale un minuto después de arrancar el contenedor**, no a
las `BOLSATECH_CADA_HORAS`. Ese minuto es para no pegarle a Oracle mientras
levanta. Conviene saberlo: encender el automático y recrear el contenedor
informa enseguida, no al día siguiente.

La pantalla dice cuándo corrió sola por última vez, cómo le fue y cuándo sería
la próxima, así que no hace falta leer logs para saber si está andando. Si una
corrida falla, también queda ahí con el error. Eso sale de
`bolsatech/ultima_automatica.json`.

**Desde cuándo busca.** La primera vez, desde `BOLSATECH_FECHA_INICIAL`.
Después, desde donde quedó, que la app se guarda en
`bolsatech/ultima_corrida.txt`. A ese desde se le restan
`BOLSATECH_SOLAPAMIENTO_MINUTOS`, para no perder un ingreso que entró justo
mientras corría la búsqueda anterior; informar de más no duplica nada porque
cada CPE se informa una sola vez.

**La fecha de corte solo avanza si salió todo bien.** Si alguna falló, o si
quedaron CPE afuera de `BOLSATECH_LIMITE`, el corte no se mueve y la corrida
siguiente vuelve a tomar ese período en vez de saltearlo. Es al revés de lo
cómodo a propósito: preferir reintentar antes que perder una CPE.

El botón **Correr ahora** hace lo mismo a mano. Sin tildar la casilla solo
muestra qué haría, sin enviar nada.

**Las fechas se pueden elegir** en esa misma pantalla. Vacías, usa el período
automático. Con fechas, informa solo ese período y **no mueve la fecha de
corte**: moverla a un rango elegido a mano saltearía todo lo que quedó entre
medio. «Hasta» vacío significa hasta ahora, y una fecha «hasta» incluye el día
entero.

### Consultar el estado de una CPE en BolsaTech

```bash
docker compose exec informarcpe python -m herramientas.consultar_cpe 10130966598
docker compose exec informarcpe python -m herramientas.consultar_cpe --xml 10130966598
```

Solo lee: ninguna de esas llamadas registra, modifica ni anula nada.

El servicio identifica la carta de porte con `numeroCartaPorte|ctg`, así que una
consulta falla **tanto si la CPE no está como si está con otro número de carta
de porte**. Por eso la herramienta prueba tres formas —`numero|ctg`, solo el
número, solo el CTG— y muestra qué contesta cada una. Si las últimas encuentran
algo que la primera no, la CPE está en BolsaTech con un número distinto del que
arma el ERP.

De cada CPE encontrada muestra el `estado` (`INGRESADA`, `ANULADA`) y la fecha
de registración.

**`ya_informada()` no mira el estado**: da por informada cualquier CPE que el
servicio devuelva. Una `ANULADA` cuenta como informada y no se vuelve a
informar. Está así a propósito hasta confirmar con BolsaTech qué corresponde
hacer con una anulada; cambiarlo haría que la app empiece a dar de alta CPE que
hoy saltea.

Los otros dos métodos que documenta Bolsatech y la app **no** usa: `getCTG`
(del número de carta de porte al CTG) y `requiereMuestra` (si esa CPE va a
análisis).

### Volver a informar una CPE que la app ya dio por informada

La app no informa dos veces la misma CPE: lo que quedó como `informada` o
`ya_estaba` en `estado.json` se saltea para siempre. Eso es lo que se quiere
casi siempre, salvo cuando **una CPE se anula y hay que volver a cargarla**, o
cuando se cargó con el participante equivocado.

```bash
docker compose exec informarcpe python -m herramientas.olvidar_cpe 10130966598
docker compose exec informarcpe python -m herramientas.olvidar_cpe --confirmar 10130966598
```

No toca nada en BolsaTech: sólo borra la anotación local para que la próxima
vez se vuelva a intentar. Si del otro lado la CPE sigue existiendo, el alta
rebota igual y vuelve a quedar como `ya_estaba` — esa es la red de seguridad
real, no la anotación.

Antes de cambiar nada deja un respaldo `estado_AAAAMMDD_HHMMSS.json` al lado,
así volver atrás es copiarlo encima.

**Cada participante ve sólo lo suyo.** `consultarCartasDePorte` devuelve
únicamente las CPE de la cuenta con la que se consulta, pero el CTG es único
en todo BolsaTech. Una CPE cargada por otro participante —otra sociedad del
grupo, otro acopio, el productor— no aparece en la consulta y sin embargo hace
rebotar el alta con "ya existe una carta de porte con ese CTG". Por eso
`ya_informada()` no alcanza como única defensa y la de verdad es el rechazo
del alta.

### Confirmar los trackId de los logs viejos

Los scripts corrieron meses sin `BOLSATECH_TRACK_URL`, así que cada alta quedó
con su `trackId` sin confirmar y lo único que queda de eso son los `.log`. Para
confirmarlos de una vez hay una herramienta aparte, que lee esos logs:

```bash
# 1. Dejar los .log donde el contenedor los vea
sudo mkdir -p /storage/informarcpe/logs
sudo cp ejecuciones.log ejecuciones_desde_csv.log /storage/informarcpe/logs/

# 2. Ver qué haría, sin enviar nada
docker compose exec informarcpe python -m herramientas.confirmar_desde_log \
    /data/logs/ejecuciones.log /data/logs/ejecuciones_desde_csv.log

# 3. Confirmar de verdad
docker compose exec informarcpe python -m herramientas.confirmar_desde_log \
    --confirmar /data/logs/ejecuciones.log /data/logs/ejecuciones_desde_csv.log
```

Confirmar un `trackId` **no da de alta ninguna CPE**: solo acusa recibo, así
que correrlo de más no duplica nada. El avance se guarda en
`bolsatech/confirmacion_log.json` después de cada `trackId`, así que si se corta
se puede volver a correr y retoma donde quedó, sin repetir lo que ya salió bien.

Con qué cuenta firmar sale de las líneas `CREDENCIALES` del log; para los CTG
que el log no aclara se busca el `id_muestra` en el ERP y, si tampoco está, se
prueban las dos cuentas. Con `--limite N` se hace de a poco y con `--sin-oracle`
no se consulta el ERP.

### Qué se informa de cada CPE

| Campo de BolsaTech | De dónde sale |
|---|---|
| `numeroCartaPorte` | sucursal interna + número interno en 8 posiciones (9 dígitos) |
| `ctg` | `IPL_CTG` |
| `titular` y `remitenteComercial` | CUIT y nombre del productor |
| `destinatario` y `destino` | por planta, de `BOLSATECH_DESTINATARIOS` |
| `codigoEspecie` | `ESP_CODIGO_SAGPYA` de la especie del ERP (23 = soja) |
| `establecimiento` | `CAM_DESCRIPCION` del campo |
| `codigoLocalidadProcedencia` | `CPO_LOCALIDAD_ONCCA` del código postal del campo |
| `numeroPlantaDestino` | `PLA_NUMERO_ONCCA` |
| `codigoLocalidadDestino` | por planta, de `BOLSATECH_PLANTAS` |
| `idMuestra` | `INB_IDENTIFICADOR_MUESTRA`, o prefijo + CTG |
| `kilosNetosSecos` | `IPL_PESO_NETO`, redondeado |
| `fechaHoraConfirmacion` | `IPL_FECHA_HORA` |

Los scripts mandaban `codigoEspecie` fijo en `23`. Acá sale de
`ESP_CODIGO_SAGPYA`. Verificado contra `SYSADMIN.ESPECIES`: las cinco especies
de soja del ERP dan todas 23.

| `ESP_ESPECIE` | `ESP_DESCRIPCION` | `ESP_CODIGO_SAGPYA` |
|---|---|---|
| 08 | Soja | 23 |
| 14 | Soja Orgánica | 23 |
| 20 | Soja  Buffer | 23 |
| 24 | Soja ESP | 23 |
| 38 | Soja EUDR | 23 |

`BOLSATECH_ESPECIE_DEFECTO` queda como red de seguridad para cuando el ERP no
lo trae: si mañana cargan una sexta variante de soja y se olvidan ese campo, la
CPE se informa igual en vez de quedar afuera.

Ojo que `Soja  Buffer` tiene dos espacios en el ERP y `Soja Orgánica` lleva
tilde. Para BolsaTech no importa, porque el cruce es por `ESP_ESPECIE`. Para
las equivalencias de Albor tampoco: ahí el cruce por nombre pasa por
`_normalizar`, que colapsa los espacios y saca las tildes. Lo que sí conviene
es **no corregirlos en el ERP**: la clave guardada en `equivalencias.json` es
el nombre crudo, así que cambiarlo hace que esa especie se pregunte una vez
más.

Una CPE a la que le falte un dato obligatorio **no se informa**: aparece en la
pantalla con el motivo, para corregirla en el ERP.

## Informar CPE a Visec por API

Cuarta sección del menú, **Visec por API**. Hace lo mismo que el XLSX de
*Informar Visec* pero sin planilla: lee el ERP, completa con ARCA y manda cada
CPE a la API de trazabilidad B2B de Visec, sola cada pocos minutos. El módulo
del XLSX sigue funcionando igual, aparte.

### Por qué es menos complejo de lo que parece

El JSON de la API es, campo por campo y en el mismo orden, la planilla de 25
columnas. Así que este módulo no traduce nada de nuevo: arma cada CPE con
`visec.armar_fila`, la misma función que llena el XLSX, y solo convierte esas
columnas a JSON con los tipos que pide la API (enteros para RUCA, producto y
pesos por UP; texto para el resto; fechas ISO; campaña `25/26`).

| De dónde | Qué |
|---|---|
| Oracle | qué CTG informar (especie `VISEC_API_ESPECIES`, por fecha) y lo que ARCA no tiene: fecha del movimiento, peso que entró a stock, kilos estimados, RENSPA. Las mismas columnas que `ctg_soja.py`. |
| ARCA | el resto de la CPE: intervinientes, RUCA, producto, campaña, pesos de carga. |
| Visec | recibe el JSON y lo procesa asincrónicamente. |

### Los endpoints

De los doce de la API, a un acopio le corresponden cinco. Se usan dos:

| Endpoint | Para qué |
|---|---|
| `ProcesoImportacionJsonCartaPorte` | el alta. Devuelve un número de proceso, no el resultado. |
| `ConsultarProcesoImportacionCartaPorte` | el resultado de cada CTG de ese proceso. |

Los de stock (`ExisteStockRUCA`, `ExisteStockUnidadProductiva`) sirven para
validar un camión cuando llega, que es otro momento. Según la gente del ERP no
hacen falta: al llevar la soja Visec como especie propia, la validación queda
implícita en la distribución de campo y en el contrato.

La autenticación es OAuth2 *client credentials*: con `client_id` y
`client_secret` por Basic auth se pide un token que dura 8 horas, y cada
llamada lleva ese token más el header `current-company` con el CUIT. La app lo
guarda en memoria y lo renueva sola cinco minutos antes de que venza.

### Staging y producción

Mismo contrato, distinto host y distinto scope del token. Se elige con
`VISEC_API_AMBIENTE=stg|prd`, y **las credenciales son distintas en cada
uno**. Arranca en staging, y la pantalla lo muestra arriba a la derecha.

### Qué se manda, y qué no

- **Solo CPE confirmadas o descargadas en ARCA (CN, DD).** Con el XLSX una
  persona ve el aviso de "pesos no definitivos" y decide; acá nadie mira. Una
  CPE todavía activa queda *esperando a ARCA* y se manda sola cuando se confirme.
- **Qué va en cada movimiento depende de si es ingreso o egreso**, como en
  los ejemplos de la documentación de Visec:

  | Ejemplo de Visec | RENSPA | CTG asignado | Carga por UP | Descarga y stock |
  |---|---|---|---|---|
  | A – ingreso desde unidad productiva | sí | — | — | sí |
  | B – egreso de un sitio | — | sí | sí | — |
  | C – ingreso desde otro sitio | — | — | — | sí |

  Los ingresos a planta son el caso A o C: **no van ni el CTG asignado ni la
  carga por UP**. El XLSX pone el mismo CTG como asignado ("criterio de
  Rivara") y la carga por UP de `IPL_KILOS_ESTIMADOS`; por API, Visec rechaza
  lo primero, y lo segundo sería un estimado (30.000 contra 22.235 reales en la
  primera prueba). El peso real va en descarga y en stock. La carga de la CPE
  (`pesoNetoCargaKg`) sí va, y es la de ARCA: es el dato del documento.
- **Lo que tenga 0 no va.** ARCA da `planta 0` cuando el origen es un productor,
  y un RUCA 0 Visec lo rechaza. Lo mismo para producto y pesos.
- **Si falta algo obligatorio, no se manda**: queda *con datos faltantes* y se
  reintenta en cada corrida, por si el dato aparece.
- **Si viene de un productor, el RENSPA es obligatorio.** El esquema del
  OpenAPI no lo marca, pero la descripción de `movimientoMercaderiaUP` sí: si
  la CPE viene de un productor (sin RUCA de origen), cada movimiento es una
  unidad productiva con su RENSPA. Si viene de un acopio, en cambio, va el CTG
  asignado. El caso típico es un operario que no cargó el RENSPA en el ingreso:
  queda con datos faltantes y sale sola en cuanto se corrige en el ERP.

### El ciclo

Cada corrida, primero pregunta el resultado de los procesos que quedaron
esperando, y después busca lo nuevo. Mira los últimos `VISEC_API_VENTANA_DIAS`
días (nunca antes de `VISEC_API_FECHA_INICIAL`); lo ya registrado se saltea por
el estado local, en `/storage/informarcpe/visec_api/estado.json`. No hay fecha
de corte como en BolsaTech: la ventana móvil hace que una CPE que tarda en
confirmarse en ARCA no se pierda.

Los envíos van de a `VISEC_API_LOTE`. Si Visec rechaza un lote entero en la
prevalidación, se manda de a una, así una CPE mal no frena a las demás.

| Estado | Qué significa | ¿Se reintenta sola? |
|---|---|---|
| registrada | Visec la registró | — |
| enviada | está en un proceso, falta el resultado | se pregunta en la próxima |
| rechazada | Visec dijo que no | **no**: se reintenta a mano |
| esperando a ARCA | ARCA todavía no la confirmó | sí |
| con datos faltantes | le falta algo obligatorio | sí |
| con error | red, ARCA o Visec no respondieron | sí |

**Las rechazadas no se reintentan solas**, a propósito: si Visec rechazó porque
un RENSPA o un RUCA no está dado de alta, mandarla cada 15 minutos no lo
arregla. Se corrige del lado de Visec y se vuelve a poner en la cola desde la
pantalla. Cuando hay rechazos, la corrida automática manda un mail; cuando
sale todo bien no, porque un mail cada 15 minutos con "todo bien" deja de
leerse.

Dos corridas nunca se pisan: si la automática está corriendo y alguien aprieta
el botón, el botón avisa y no hace nada.

Pedido y respuesta de cada llamada quedan en `visec_api/respuestas/`. El token
va en las cabeceras, así que ahí no queda ningún secreto.

### Lo que todavía hay que confirmar en staging

Confirmado con la primera respuesta real (04/11/2026):

- **`fechaHoraMovimiento` va sin zona horaria**, en hora argentina:
  `2025-05-02T13:59:09`. Con `-03:00` el conversor de Visec contesta
  *"Allowed formats: 'yyyy-MM-dd'T'HH:mm:ss', 'yyyy-MM-dd'T'HH:mm:ss.FFFFFFF'"*.
- **El motivo de un 400 viene en `details`**, no en `errorMessage`, que es
  genérico ("Error al convertir JSON."). La pantalla muestra `details`.
- **`fechaCPE` como texto `AAAA-MM-DD`** pasa la conversión, aunque el OpenAPI
  la describa como objeto (así documenta el generador un `DateOnly` de .NET).
- **El CTG va con 12 dígitos**, cero adelante: `010123352370`. Con los 11 de
  ARCA contesta *"Formato de Número CTG incorrecto"*. Su respuesta de ejemplo
  también lo devuelve así (`"NumeroCTG": "010000000001"`), así que al leer el
  resultado se comparan sin los ceros de adelante.

**Los ejemplos de la documentación** (dentro del OpenAPI, en cada endpoint)
resolvieron lo que el esquema no decía: qué va en ingresos y egresos (tabla de
arriba) y los estados del proceso:

| | Valores |
|---|---|
| detalle de cada CTG | `{Id: 1, "OK"}` registrada · `{Id: 2, "ERROR"}` con `Errores` |
| `CodigoEstado` del proceso | `3` Finalizado Sin Error · `4` Finalizado Con Error |

Esos ejemplos vienen con las claves en mayúscula (`NumeroCTG`) y el 400 real
vino en minúscula (`numeroCTG`): la app lee las dos.

**Ciclo completo validado en staging el 04/11/2026** con el CTG
`10123352370`: alta (proceso 18079), seguimiento y lectura del resultado.

- El alta devuelve `numero` e `id` (18079 y 5975). Para consultar el
  resultado va `numero`.
- Las respuestas reales vienen en camelCase (`numeroCTG`, `codigoEstado`),
  no en PascalCase como los ejemplos.
- Los errores del detalle pueden traer solo `observaciones`, sin `codigoError`.
- `numeroCPE` con el formato del XLSX (`00004-00007404`) pasa.

Ese primer envío volvió rechazado por un tema de Visec, no de los datos:
*"El Número RUCA Destino ingresado no se corresponde con una Sucursal
habilitada en el sistema."* Las sucursales de Rivara en Visec (15125, 21350,
21349, 406793) figuran *Inhabilitada*, y hasta que Visec las habilite todo lo
que entre por ellas va a volver así.

### En Visec: sucursales y unidades productivas

La API exige que el RUCA de destino y el RENSPA de cada unidad productiva estén
**dados de alta y habilitados en Visec**. Si una sucursal figura *Inhabilitada*
en el portal de Visec, todo lo que entre por ella va a volver rechazado, por
más que los datos estén bien.

### Probarlo

1. **Probar la conexión**: pide un token. No manda ninguna CPE.
2. **Correr ahora** con un CTG puntual y sin tildar: muestra el JSON exacto.
3. Lo mismo tildando: lo manda a staging.
4. **Correr ahora** otra vez, sin nada: pregunta el resultado del proceso.

Cuando eso salga bien con algunas CPE, `VISEC_API_AUTOMATICO=true`.

**El automático informa el día a día; lo atrasado se manda a mano.** La
ventana mira los últimos `VISEC_API_VENTANA_DIAS` y la fecha inicial solo la
acota, nunca la estira hacia atrás. Fuera de la cosecha de soja es normal que
encuentre 0 ingresos. Para mandar una cosecha entera, *Correr ahora* con
*Desde/Hasta*.

## Qué columnas completa, y de dónde

| Columna de Visec | Origen (campos de la respuesta, manual wscpe v2.0.5) |
|---|---|
| Fecha y Hora Movimiento | CSV del script (`fecha_movimiento`); si no, `cabecera.fechaInicioEstado` |
| Fecha CPE | `cabecera.fechaEmision` |
| Número CPE | `cabecera.sucursal` + `cabecera.nroOrden` |
| Número CTG | `cabecera.nroCTG` |
| CUIT Titular | `origen.cuit` (la CPE automotor no tiene un campo "titular": es quien la emite) |
| Número RUCA Origen | `origen.planta` (o `origen.plantaARCA`) |
| CUIT Remitente Comercial Productor | `retiroProductor.cuitRemitenteComercialProductor` |
| CUIT Rte Comercial Venta Primaria / Secundaria / Secundaria 2 | `intervinientes.*` |
| CUIT Corredor Venta Primaria / Secundaria | `intervinientes.*` |
| CUIT Destinatario | `destinatario.cuit` |
| CUIT Destino | `destino.cuit` |
| Número RUCA Destino | `destino.planta` |
| Código Producto | `datosCarga.codGrano`, traducido con `MAPA_GRANO_VISEC` |
| Campaña | `datosCarga.cosecha` |
| Peso Neto Carga (Kg) | `datosCarga.pesoBruto` − `datosCarga.pesoTara` |
| Peso Ingreso Stock (Kg) | CSV del script (`peso_ingreso_stock`); si no, `datosCarga.pesoBrutoDescarga` − `pesoTaraDescarga` |
| Número RENSPA | CSV del script (`IPL_NUMERO_RENSPA`) |
| Número CTG Asignado | el mismo CTG de la CPE |
| Peso Neto Carga (Kg) por UP | CSV del script (`IPL_KILOS_ESTIMADOS`) |
| Peso Neto Descarga (Kg) por UP | el mismo valor que Peso Ingreso Stock |
| Último Almacenamiento, Tipo Movimiento | valores fijos del `.env` |

Solo una CPE en estado **CN (confirmada)** o **DD (descargada en destino)**
tiene los pesos de descarga definitivos. En cualquier otro estado la app la
incluye igual, pero con un aviso.

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

## Cómo habla con ARCA

**El manual y el servicio no coinciden.** El manual wscpe v2.0.5 reemplazó
"afip" por "arca" en todo el texto, pero el servicio que corre no cambió. Lo
que vale es lo que declara el WSDL real:

| | Manual v2.0.5 (no funciona) | WSDL real (lo que usa la app) |
|---|---|---|
| Endpoint de producción | `cpea-ws.arca.gob.ar` | `https://cpea-ws.afip.gob.ar/wscpe/services/soap` |
| Namespace | `https://serviciosjava.arca.gob.ar/wscpe/` | `https://serviciosjava.afip.gob.ar/wscpe/` |
| SOAPAction | no lo menciona | obligatorio: `<namespace><operación>` |

El balancer de ARCA no da errores: si el host, el namespace o el SOAPAction no
coinciden, contesta **HTTP 200 con el cuerpo vacío**. Por eso cuesta tanto
encontrarlo.

El SOAP se arma a mano, como hace `cpe_bolsatech.py` con BolsaTech. Si ARCA
vuelve a mover algo, se corrige desde el `.env` sin tocar código:
`ARCA_URL_WSCPE`, `ARCA_URL_WSAA` y `ARCA_NS_WSCPE`. El WSDL está en
`https://cpea-ws.afip.gob.ar/wscpe/services/soap?wsdl`, y de ahí salen el
`soap:address`, el `targetNamespace` y los `soapAction`.

### Diagnóstico desde el servidor

```bash
# ¿Llega a ARCA? No necesita certificado.
docker exec informarcpe python -m app.wscpe dummy

# Una consulta real, con la respuesta completa
docker exec informarcpe python -m app.wscpe consultar 10135055909
```

`dummy` tiene que devolver `appserver`, `authserver` y `dbserver` en `OK`.

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
