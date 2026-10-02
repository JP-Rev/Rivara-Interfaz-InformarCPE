FROM python:3.12-slim

# tzdata: las fechas que se informan son locales, no UTC.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=America/Argentina/Buenos_Aires

WORKDIR /srv

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata curl \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# Corre sin privilegios: solo necesita leer los certificados y escribir /data.
RUN useradd --create-home --uid 10001 rivara \
 && mkdir -p /data/salida /data/cache \
 && chown -R rivara:rivara /data
USER rivara

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s \
  CMD curl -fsS http://localhost:8000/salud || exit 1

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers"]
