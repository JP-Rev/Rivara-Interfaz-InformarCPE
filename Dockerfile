# bookworm explicito: en trixie la libreria libaio pasa a llamarse
# libaio.so.1t64 y el Instant Client, que busca libaio.so.1, no arranca.
FROM python:3.12-slim-bookworm

# Oracle Instant Client. Hace falta porque la base del ERP es 11.2 y el modo
# "thin" de python-oracledb (sin cliente) necesita 12.1 o posterior. El
# paquete "basiclite" alcanza: son solo consultas, sin soporte de idiomas.
ARG INSTANT_CLIENT=https://download.oracle.com/otn_software/linux/instantclient/1928000/instantclient-basiclite-linux.x64-19.28.0.0.0dbru.zip

# tzdata: las fechas que se informan son locales, no UTC.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Argentina/Buenos_Aires

WORKDIR /srv

# ldconfig y no lib_dir: en Linux el Instant Client se busca por la ruta de
# librerias del sistema, pasarselo a oracledb por parametro no alcanza.
RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata libaio1 curl unzip \
 && curl -sSLo /tmp/ic.zip "$INSTANT_CLIENT" \
 && unzip -q /tmp/ic.zip -d /opt \
 && rm /tmp/ic.zip \
 && echo /opt/instantclient_19_28 > /etc/ld.so.conf.d/oracle.conf \
 && ldconfig \
 && apt-get purge -y curl unzip \
 && apt-get autoremove -y \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
# El script con SQL*Plus no corre en el contenedor: esta para que los admins lo
# descarguen desde la app. Forma JSON porque el .bat tiene un espacio.
COPY ["scripts/ctg_soja.py", "scripts/Generar CTG.bat", "./scripts/"]

# Corre sin privilegios: solo lee los certificados y escribe en /data.
# El UID queda fijo porque /storage/informarcpe del host tiene que ser suyo
# (contrato §7: "los contenedores escriben con el UID de su proceso").
RUN useradd --create-home --uid 10001 rivara
USER rivara

# Puerto 80 dentro del contenedor (contrato §2). Un proceso sin privilegios no
# puede escuchar ahi por omision: el compose levanta la restriccion con el
# sysctl net.ipv4.ip_unprivileged_port_start.
EXPOSE 80

# El healthcheck va en el compose, como en los moldes del contrato.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "80", "--proxy-headers"]
