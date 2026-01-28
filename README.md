# Speedtest Monitor

Aplikacja do monitoringu jakości łącza internetowego z interfejsem webowym.

## Funkcje

- Monitoring łączności z internetem (ping TCP co sekundę)
- Testy prędkości pobierania (URL/FTP lub speedtest.net/speedtest.pl)
- Wykres historii online/offline i prędkości
- Eksport danych do CSV
- Raport jakości usługi (awarie + czas niedostępności)
- Powiadomienia email (SMTP) o awariach i przywróceniu łączności
- Interfejs webowy (bez logowania)

## Obrazy Docker

- **GHCR**: [ghcr.io/r4vk/speedtest](https://ghcr.io/r4vk/speedtest)
- **Docker Hub**: [r4vk/speedtest](https://hub.docker.com/r/r4vk/speedtest)

## Szybki start

### Docker Compose (zalecane)

```yaml
services:
  speedtest:
    image: ghcr.io/r4vk/speedtest:latest
    container_name: speedtest
    restart: unless-stopped
    ports:
      - "8000:8000"
    volumes:
      - speedtest-data:/data
      - /etc/localtime:/etc/localtime:ro
    environment:
      - TZ=Europe/Warsaw

volumes:
  speedtest-data:
```

```bash
docker-compose up -d
```

UI dostępne pod: `http://localhost:8000`

### Docker run

```bash
docker run -d --name speedtest \
  -p 8000:8000 \
  -v speedtest-data:/data \
  -v /etc/localtime:/etc/localtime:ro \
  -e TZ=Europe/Warsaw \
  ghcr.io/r4vk/speedtest:latest
```

## Konfiguracja

Większość ustawień można zmienić w UI (Ustawienia). Zmienne środowiskowe działają jako wartości domyślne.

### Podstawowe

| Zmienna | Domyślnie | Opis |
|---------|-----------|------|
| `TZ` | - | Strefa czasowa (np. `Europe/Warsaw`) |
| `PORT` | `8000` | Port serwera HTTP |
| `DATA_DIR` | `/data` | Katalog z bazą danych |

### Monitoring łączności

| Zmienna | Domyślnie | Opis |
|---------|-----------|------|
| `CONNECT_TARGET` | `google.com` | Host do sprawdzania łączności |
| `CONNECT_DEFAULT_PORT` | `443` | Port TCP |
| `CONNECT_INTERVAL_SECONDS` | `1` | Interwał sprawdzania (sekundy) |
| `CONNECT_TIMEOUT_SECONDS` | `1` | Timeout połączenia |

### Test prędkości

| Zmienna | Domyślnie | Opis |
|---------|-----------|------|
| `SPEEDTEST_URL` | - | URL pliku do pobierania (http/https/ftp) |
| `SPEEDTEST_DURATION_SECONDS` | `10` | Czas trwania testu |
| `SPEEDTEST_INTERVAL_SECONDS` | `900` | Interwał między testami (15 min) |
| `SPEEDTEST_SKIP_IF_OFFLINE` | `true` | Pomiń test gdy offline |

### Powiadomienia email (SMTP)

| Zmienna | Domyślnie | Opis |
|---------|-----------|------|
| `SMTP_HOST` | - | Serwer SMTP |
| `SMTP_PORT` | `587` | Port SMTP |
| `SMTP_USER` | - | Login SMTP |
| `SMTP_PASSWORD` | - | Hasło SMTP |
| `SMTP_FROM` | = `SMTP_USER` | Adres nadawcy |
| `SMTP_TO` | - | Adres odbiorcy powiadomień |
| `SMTP_USE_TLS` | `true` | Używaj STARTTLS |
| `SMTP_MIN_OUTAGE_SECONDS` | `60` | Minimalna długość awarii do wysłania maila |

Powiadomienia są wysyłane **tylko po przywróceniu** internetu (gdy awaria się skończy i trwała dłużej niż `SMTP_MIN_OUTAGE_SECONDS`).

#### Przykład z Gmail

```bash
-e SMTP_HOST=smtp.gmail.com \
-e SMTP_PORT=587 \
-e SMTP_USER=twoj-email@gmail.com \
-e SMTP_PASSWORD=xxxx-xxxx-xxxx-xxxx \
-e SMTP_TO=odbiorca@example.com
```

Gmail wymaga [App Password](https://support.google.com/accounts/answer/185833).

## Persystencja danych

Baza SQLite jest przechowywana w `/data`. Zamontuj volume żeby dane przetrwały restarty i aktualizacje:

```bash
-v speedtest-data:/data
```

## Aktualizacja

```bash
docker-compose pull
docker-compose up -d
```

## API

- `GET /api/status` - aktualny status
- `GET /api/speed?from=...&to=...` - historia prędkości
- `GET /api/outages?from=...&to=...` - historia awarii
- `GET /api/pings?from=...&to=...` - historia pingów
- `GET /api/export/speed.csv` - eksport prędkości
- `GET /api/export/outages.csv` - eksport awarii
- `GET /api/export/pings.csv` - eksport pingów
- `GET /api/report/quality?from=...&to=...` - raport jakości

Daty w formacie ISO-8601: `2026-01-28T00:00:00Z`

## Licencja

[PolyForm Noncommercial 1.0.0](LICENSE) - możesz używać i modyfikować, ale nie komercyjnie.
