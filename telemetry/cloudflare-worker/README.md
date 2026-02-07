# Cloudflare Worker - anonimowa telemetryka startów

Ten Worker zbiera tylko:

- `install_id`
- `version`
- `event` (tu: `app_started`)
- `started_at`

## 1) Wymagania

- konto Cloudflare
- zainstalowany `wrangler` (CLI)

```bash
npm install -g wrangler
wrangler login
```

## 2) Utwórz bazę D1

```bash
wrangler d1 create speedtest-telemetry
```

Skopiuj `database_id` z outputu.

## 3) Skonfiguruj Workera

```bash
cd telemetry/cloudflare-worker
cp wrangler.toml.example wrangler.toml
```

W pliku `wrangler.toml` wstaw `database_id`.

## 4) Załóż tabelę

```bash
wrangler d1 execute speedtest-telemetry --remote --file=schema.sql
```

## 5) Ustaw sekrety

```bash
wrangler secret put INGEST_TOKEN
wrangler secret put ADMIN_TOKEN
```

- `INGEST_TOKEN` - token, którym aplikacja wysyła eventy
- `ADMIN_TOKEN` - token do endpointu `/stats`

## 6) Deploy

```bash
wrangler deploy
```

Po deployu dostaniesz URL np. `https://speedtest-telemetry.<twoja-subdomena>.workers.dev`.

## 7) Konfiguracja aplikacji Speedtest

W `docker-compose.yml` aplikacji dodaj:

```yaml
environment:
  - TELEMETRY_ENDPOINT=https://speedtest-telemetry.<twoja-subdomena>.workers.dev/collect
  - TELEMETRY_AUTH_TOKEN=<INGEST_TOKEN>
  - TELEMETRY_DEFAULT_ENABLED=true
```

Telemetryka jest `opt-out`:

- domyślnie włączona (`TELEMETRY_DEFAULT_ENABLED=true`)
- user może ją wyłączyć w UI: Ustawienia -> Anonimowa telemetryka

## 8) Podgląd statystyk

```bash
curl -H "Authorization: Bearer <ADMIN_TOKEN>" \
  https://speedtest-telemetry.<twoja-subdomena>.workers.dev/stats
```

## Endpointy

- `POST /collect` - zapis eventu startu
- `GET /stats` - zagregowane statystyki (`Authorization: Bearer <ADMIN_TOKEN>`)
- `GET /healthz` - healthcheck
