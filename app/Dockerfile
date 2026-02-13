FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DATA_DIR=/data \
    PORT=8000 \
    TZ=Europe/Warsaw

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN set -eux; \
    export DEBIAN_FRONTEND=noninteractive; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
      tzdata \
      traceroute \
      mtr-tiny \
      nmap \
      iproute2 \
      iputils-ping \
      iperf3; \
    rm -rf /var/lib/apt/lists/* /var/cache/apt/*; \
    rm -rf /usr/share/doc/* /usr/share/man/* /usr/share/info/* /usr/share/locale/*
RUN pip install --no-cache-dir --no-compile -r /app/requirements.txt

COPY speedtest_app /app/speedtest_app
COPY static /app/static

EXPOSE 8000

CMD ["sh","-c","uvicorn speedtest_app.main:app --host 0.0.0.0 --port ${PORT}"]
