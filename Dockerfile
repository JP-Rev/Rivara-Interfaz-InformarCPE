FROM python:3.12-slim

# tzdata: las fechas que se informan son locales, no UTC.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Argentina/Buenos_Aires

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata \
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
