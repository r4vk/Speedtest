# Cloudflare Worker - anonimowa telemetryka

Ten Worker zbiera tylko:

- `install_id`
- `version`
- `event` (`app_started` lub `app_active`)

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
wrangler secret put ADMIN_TOKEN
```

- `ADMIN_TOKEN` - token do endpointu `/stats`

## 6) Deploy

```bash
wrangler deploy
```

Po deployu dostaniesz URL np. `https://speedtest-telemetry.<twoja-subdomena>.workers.dev`.

## 7) Podgląd statystyk

```bash
curl -H "Authorization: Bearer <ADMIN_TOKEN>" \
  https://speedtest-telemetry.<twoja-subdomena>.workers.dev/stats
```

## 8) Podpięcie aplikacji

Domyślny endpoint telemetryki jest zaszyty w aplikacji w `app/speedtest_app/telemetry.py` (`DEFAULT_TELEMETRY_ENDPOINT`).
Jeśli chcesz, aby Twoja wersja aplikacji wysyłała do innego Workera, zmień ten URL i zbuduj obraz ponownie.

## Endpointy

- `POST /collect` - zapis eventu (zwraca `204` przy sukcesie)
- `GET /stats` - zagregowane statystyki (`Authorization: Bearer <ADMIN_TOKEN>`)
- `GET /healthz` - healthcheck
