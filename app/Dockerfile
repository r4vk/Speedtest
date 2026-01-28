FROM python:3.12-slim

ARG APP_VERSION=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DATA_DIR=/data \
    PORT=8000 \
    APP_VERSION=${APP_VERSION}

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY speedtest_app /app/speedtest_app
COPY static /app/static

EXPOSE 8000

CMD ["sh","-c","uvicorn speedtest_app.main:app --host 0.0.0.0 --port ${PORT}"]
