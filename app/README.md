# Speedtest (kontener na Synology)

Minimalistyczna aplikacja (FastAPI + SQLite) do:

- monitoringu łączności z internetem co `CONNECT_INTERVAL_SECONDS`,
- testu prędkości pobierania co `SPEEDTEST_INTERVAL_SECONDS`,
- podglądu wykresu w UI,
- eksportu CSV oraz raportu jakości usługi (awarie + czas niedostępności),
- powiadomień email (SMTP) o awariach i przywróceniu łączności.

## Konfiguracja

Najprościej: uruchom kontener i ustaw wartości w UI w sekcji **Ustawienia** (zapisywane w SQLite, działa bez restartu).

ENV dalej działa jako wartości startowe / domyślne.

### Monitoring internetu

- `CONNECT_TARGET` (domyślnie: `google.com`)
- `CONNECT_DEFAULT_PORT` (domyślnie: `443`)
- `CONNECT_TIMEOUT_SECONDS` (domyślnie: `1`)
- `CONNECT_INTERVAL_SECONDS` (domyślnie: `5`)
- `CONNECTIVITY_CHECK_BUFFER_SECONDS` (domyślnie: `600`) – buforowanie pingów w pamięci; zapis do SQLite co N sekund
- `CONNECTIVITY_CHECK_BUFFER_MAX` (domyślnie: `300`) – maks. liczba pingów w buforze (flush po przekroczeniu)

Test łączności jest robiony przez szybkie połączenie TCP (host/URL → host+port) – działa w kontenerze bez uprawnień do ICMP.

W UI możesz też wybrać gotowy tryb: `speedtest.net` lub `speedtest.pl` (wtedy nie podajesz URL/host).

### Test prędkości pobierania

- `SPEEDTEST_URL` (domyślnie: puste) – `ftp://...` lub `http(s)://...`
- `SPEEDTEST_DURATION_SECONDS` (domyślnie: `10`)
- `SPEEDTEST_INTERVAL_SECONDS` (domyślnie: `900`)
- `SPEEDTEST_TIMEOUT_SECONDS` (domyślnie: `10`)
- `SPEEDTEST_SKIP_IF_OFFLINE` (domyślnie: `true`)

### Dane i serwer

- `DATA_DIR` (domyślnie: `/data`) – tu trzymany jest `app.db`
- `PORT` (domyślnie: `8000`)
- `TZ` – strefa czasowa dla "czasów lokalnych" w UI/CSV (np. `Europe/Warsaw`). Jeśli nie ustawione, kontener próbuje wykryć strefę z hosta.

#### Strefa czasowa na Synology DSM 7.1+

Aby kontener używał strefy czasowej z DSM, zamontuj `/etc/localtime` z hosta:

```bash
docker run -d --name r4vk-speedtest \
  -v /etc/localtime:/etc/localtime:ro \
  -e TZ=Europe/Warsaw \
  ...
```

W Container Manager (DSM 7.2+): Volume Settings → Add File → `/etc/localtime` → `/etc/localtime` (Read-Only).

### Powiadomienia email (SMTP)

Aplikacja może wysyłać powiadomienia email o awariach i przywróceniu łączności. Konfiguracja przez zmienne środowiskowe:

- `SMTP_HOST` – serwer SMTP (np. `smtp.gmail.com`, `smtp-mail.outlook.com`, `poczta.interia.pl`)
- `SMTP_PORT` – port SMTP (domyślnie: `587`)
- `SMTP_USER` – login/email do autoryzacji
- `SMTP_PASSWORD` – hasło (dla Gmail użyj "App Password")
- `SMTP_FROM` – adres nadawcy (opcjonalnie, domyślnie = `SMTP_USER`)
- `SMTP_TO` – adres odbiorcy powiadomień
- `SMTP_USE_TLS` – czy używać STARTTLS (domyślnie: `true`)
- `SMTP_MIN_OUTAGE_SECONDS` – minimalna długość awarii do wysłania maila (domyślnie: `60`)

Powiadomienia są wysyłane **tylko po przywróceniu** internetu (gdy awaria się skończy i trwała dłużej niż `SMTP_MIN_OUTAGE_SECONDS`).

#### Przykłady konfiguracji

**Gmail** (wymaga [App Password](https://support.google.com/accounts/answer/185833)):
```bash
-e SMTP_HOST=smtp.gmail.com \
-e SMTP_PORT=587 \
-e SMTP_USER=twoj-email@gmail.com \
-e SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx \
-e SMTP_TO=odbiorca@example.com
```

**Outlook/Live**:
```bash
-e SMTP_HOST=smtp-mail.outlook.com \
-e SMTP_PORT=587 \
-e SMTP_USER=twoj-email@outlook.com \
-e SMTP_PASSWORD=twoje-haslo \
-e SMTP_TO=odbiorca@example.com
```

**Interia**:
```bash
-e SMTP_HOST=poczta.interia.pl \
-e SMTP_PORT=587 \
-e SMTP_USER=twoj-email@interia.pl \
-e SMTP_PASSWORD=twoje-haslo \
-e SMTP_TO=odbiorca@example.com
```

## Uruchomienie lokalnie

```bash
cd Speedtest/app
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

export SPEEDTEST_URL='ftp://user:pass@host/path/to/file.iso'
uvicorn speedtest_app.main:app --reload --port 8000
```

UI: `http://localhost:8000`

## Docker (Synology)

### Docker Compose (zalecane)

```bash
cd Speedtest/app
docker-compose up -d
```

Dane są przechowywane w named volume `speedtest-data` i przetrwają restarty oraz aktualizacje obrazu.

### Aktualizacja do nowej wersji

```bash
docker-compose pull
docker-compose up -d
```

Dane w `/data` (baza SQLite) są zachowywane dzięki volume.

### Build lokalny

```bash
cd Speedtest/app
docker build -t r4vk-speedtest:latest .
```

### Run (bez compose)

```bash
docker run -d --name r4vk-speedtest \
  -p 8000:8000 \
  -v r4vk-speedtest-data:/data \
  -v /etc/localtime:/etc/localtime:ro \
  -e TZ=Europe/Warsaw \
  ghcr.io/r4vk/speedtest:latest
```

**Ważne:** Volume `-v r4vk-speedtest-data:/data` zapewnia persystencję danych między restartami i aktualizacjami.

## API (skrót)

- `GET /api/status`
- `GET /api/speed?from=...&to=...`
- `GET /api/outages?from=...&to=...`
- `GET /api/report/quality?from=...&to=...`
- `GET /api/pings?from=...&to=...`
- `GET /api/export/speed.csv?from=...&to=...`
- `GET /api/export/outages.csv?from=...&to=...`
- `GET /api/export/pings.csv?from=...&to=...`

Daty: ISO-8601, np. `2026-01-28T00:00:00Z`.
