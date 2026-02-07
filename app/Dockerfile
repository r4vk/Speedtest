FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8000 \
    TZ=Europe/Warsaw

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN apt-get update && apt-get install -y --no-install-recommends \
    tzdata \
    traceroute \
    mtr-tiny \
    nmap \
    iproute2 \
    dnsutils \
    whois \
    iputils-ping \
    iperf3 \
    net-tools \
  && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY speedtest_app /app/speedtest_app
COPY static /app/static

EXPOSE 8000

CMD ["sh","-c","uvicorn speedtest_app.main:app --host 0.0.0.0 --port ${PORT}"]
