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

### Anonimowa telemetryka (opt-out)

Telemetryka jest domyślnie włączona (opt-out) i można ją wyłączyć w UI.
Zakres danych: `install_id`, `version`, `event` (`app_started` oraz `app_active` wysyłany nie częściej niż raz na ~dobę).

- `TELEMETRY_DEFAULT_ENABLED` - domyślnie `true` (można wyłączyć w UI)
- `TELEMETRY_TIMEOUT_SECONDS` - domyślnie `2`

Przykładowy backend telemetryki na Cloudflare: `../telemetry/cloudflare-worker/README.md`

## Monitoring jakości łącza

Oprócz prostego "up/down" aplikacja mierzy jakość łącza kilkoma niezależnymi metodami:

- **ICMP echo** do bramy domowej (`GATEWAY_HOST`) oraz do publicznych resolverów DNS (`1.1.1.1`, `8.8.8.8`, `9.9.9.9`) — najniższy poziom, widzi problemy samego łącza.
- **TCP** — dotychczasowy test łączności (`legacy-tcp`), zachowany dla ciągłości historii.
- **DNS** — zapytanie do resolvera systemowego.
- **HTTPS** — pełne żądanie do `cloudflare.com/cdn-cgi/trace` i `google.com/generate_204`.

Każdy protokół ma własne, osobne metryki (strata pakietów, RTT p50/p95/p99, długość serii błędów) — nie są one uśredniane między sobą.

> **Uwaga — pomiar z NAS-a po kablu:** wszystkie pomiary wykonuje kontener na NAS-ie podłączonym kablem do routera. Nie mierzą one Wi-Fi ani innych urządzeń w sieci domowej — problem z Wi-Fi może nie być widoczny w tych danych.

### Konfiguracja monitoringu

- `GATEWAY_HOST` (domyślnie: puste) — adres bramy domowej; puste = cel `gateway` pozostaje wyłączony (aplikacja celowo nie zgaduje domyślnej trasy kontenera jako "bramy domowej"). Zmienna jest stosowana **przy każdym starcie**, ale tylko dopóki host celu `gateway` jest pusty: adres ustawiony w UI albo przez API nigdy nie jest nadpisywany przez restart. Wartość, która nie jest poprawnym hostem/IP, jest odrzucana z ostrzeżeniem w logu, a cel zostaje wyłączony.
- Cele pomiarowe (targets) edytuje się w UI albo przez `GET/POST /api/targets` i `PUT/DELETE /api/targets/{id}`; domyślny zestaw jest zakładany automatycznie przy pierwszym uruchomieniu
- `PROBE_MAX_CONCURRENCY` (domyślnie: `16`) — ile sond może biec równolegle
- `PROBE_FLUSH_SECONDS` (domyślnie: `5`) i `PROBE_FLUSH_MAX` (domyślnie: `500`) — bufor zapisu wyników do SQLite, patrz niżej
- `PROBE_BUFFER_HARD_MAX` (domyślnie: `20000`) — twardy limit wierszy trzymanych w pamięci
- `DEVICE_ID` (domyślnie: `nas`) — identyfikator tego urządzenia pomiarowego
- Progi incydentów, okno oceny dostępności, testy obciążeniowe (iperf3), limity diagnostyki (mtr) i wszystkie okresy retencji konfiguruje się w UI (zapisywane w SQLite, działa bez restartu)

Test obciążeniowy (upload/download) wymaga **osobnego serwera iperf3** w tej samej sieci (np. `iperf3 -s` na innym urządzeniu) — kontener zawiera już klienta `iperf3`, ale nie uruchamia własnego serwera.

### Bufor zapisu i utrata danych przy nagłym zatrzymaniu

Wyniki sond trafiają najpierw do bufora w pamięci i są zapisywane do SQLite co `PROBE_FLUSH_SECONDS` (domyślnie 5 s) albo gdy bufor osiągnie `PROBE_FLUSH_MAX` wierszy (domyślnie 500) — zależnie co nastąpi pierwsze. Nieudany zapis jest ponawiany z rosnącym opóźnieniem (1, 2, 4, 8… aż do 30 s za każdym razem), a bufor w tym czasie rośnie do najwyżej `PROBE_BUFFER_HARD_MAX` wierszy (domyślnie 20 000); po przekroczeniu tego limitu najstarsze wiersze są odrzucane, a ich liczba jest widoczna w `/api/quality/status` jako `scheduler.dropped_rows`.

**W praktyce:** przy nagłym zatrzymaniu kontenera (np. `docker kill`, zanik zasilania) można stracić co najwyżej `PROBE_FLUSH_SECONDS` sekund świeżo zebranych wyników sond — plus to, co akurat było zatrzymane w buforze podczas nieudanego zapisu (do `PROBE_BUFFER_HARD_MAX`). Incydenty i agregaty godzinowe/dzienne są zapisywane od razu (synchronicznie) i tej utracie nie podlegają.

### ICMP w kontenerze na Synology

Przy starcie aplikacja wykrywa, jaką metodą może wysyłać ICMP echo, i pokazuje wynik w `/api/quality/status` jako `icmp_method`:

1. `dgram` — niewymagający uprawnień socket ICMP typu datagram (najczęstszy, najlepszy przypadek)
2. `raw` — surowy socket ICMP (wymaga uprawnień)
3. `ping` — zewnętrzny plik binarny `ping` jako fallback
4. `unavailable` — ICMP jest całkowicie niedostępne; cele ICMP zaczynają raportować błędy (`error_kind` = `permission`/`exec`), reszta monitoringu (TCP/DNS/HTTPS) działa bez zmian

Jeśli `icmp_method` pokazuje `ping` albo `unavailable`, odkomentuj jedną z opcji w `docker-compose.yml`:

```yaml
cap_add:
  - NET_RAW
# albo, zamiast cap_add:
sysctls:
  - net.ipv4.ping_group_range=0 2147483647
```

**Uwaga:** to zachowanie **nie zostało zweryfikowane na prawdziwym Synology NAS** w tym wydaniu — patrz checklist na końcu tej sekcji.

### Retencja danych i wzrost bazy

Zanim surowe wyniki sond (`probe_results`) zostaną usunięte, są agregowane godzinowo i dziennie — historia jakości łącza jest więc dostępna bezterminowo, tylko z mniejszą rozdzielczością po upływie okresu retencji danych surowych.

| Dane | Domyślna retencja | Ustawienie |
|---|---|---|
| Surowe wyniki sond (`probe_results`) | 14 dni | `retention_raw_days` |
| Agregaty godzinowe/dzienne (`probe_aggregates`) | 365 dni | `retention_aggregate_days` |
| Incydenty (`incidents`) | 730 dni | `retention_incident_days` |
| Surowe dane testu obciążeniowego (`load_tests.raw_json`) | 90 dni | `retention_load_test_raw_days` |
| Diagnostyka (`diagnostics`) | 365 dni | `retention_diagnostics_days` |

> **Uwaga do tabeli:** próg usuwania danych surowych jest zaokrąglany w dół do początku doby UTC, żeby kubełek agregatu nigdy nie stracił części swoich wierszy surowych. W praktyce surowe wiersze żyją do ok. 24 h **dłużej** niż `retention_raw_days` (nigdy krócej) — 14 dni to gwarantowane minimum, nie twarda granica.

Zadanie retencji uruchamia się co godzinę (pierwszy raz 60 s po starcie aplikacji). Bieżący stan i szacowany wzrost bazy zwraca `GET /api/quality/retention`.

**Rząd wielkości:** jeden cel pomiarowy pingowany co 1 s to ok. 86 400 wierszy `probe_results` dziennie. Realny przyrost w bajtach na dzień (zależny od liczby aktywnych celów i ich interwałów) zwraca pole `estimated_raw_bytes_per_day` w `GET /api/quality/retention`, razem z projekcją na cały okres retencji (`estimated_raw_bytes_at_retention`).

### Eksport i raport

- `GET /api/quality/report.html?from=...&to=...` — **raport do druku/PDF dla dostawcy** (ten sam, do którego prowadzi przycisk w panelu): pokrycie danych, dostępność, statystyki per cel, incydenty, wykresy i lista ograniczeń pomiaru
- `GET /api/report/quality?from=...&to=...` — starszy raport jakości łącza w JSON (zachowany dla zgodności)
- `GET /api/quality/export/probes.csv?from=...&to=...[&target_id=...]` — surowe wyniki sond
- `GET /api/quality/export/incidents.csv?from=...&to=...` — incydenty
- `GET /api/quality/export/aggregates.csv?from=...&to=...&bucket=1h|1d` — agregaty
- `GET /api/quality/export/load-tests.csv?from=...&to=...` — testy obciążeniowe

Dla zakresów starszych niż `retention_raw_days` surowe dane nie są już dostępne — raport i eksport CSV sygnalizują to zamiast pokazywać niekompletne dane bez ostrzeżenia.

**Limit zakresu danych surowych:** widoki oparte na surowych wierszach (`/api/quality/stats`, `/api/quality/timeline`, raport i `probes.csv`) obsługują zakres do **31 dni** (`raw_range_max_days` w `GET /api/quality/status`). Szerszy zakres liczony jest z agregatów i jest tak oznaczony (`data_source: "aggregates"`), a `probes.csv` odpowiada wtedy `422` — jeden klik nie może zająć całej pamięci NAS-a. Dłuższe okresy eksportuje się przez `aggregates.csv`.

### Weryfikacja na NAS-ie (do wykonania)

To wydanie **nie zostało jeszcze zweryfikowane na prawdziwym Synology NAS**. Przed dłuższym/produkcyjnym wdrożeniem sprawdź:

- [ ] jaką metodę ICMP wybiera kontener na docelowym Synology (`icmp_method` w `/api/quality/status`) i czy trzeba dodać `NET_RAW`/`sysctl` z sekcji wyżej
- [ ] czy `GATEWAY_HOST` wskazuje faktyczną bramę domową, a nie domyślną trasę kontenera
- [ ] zużycie CPU/RAM/dysku podczas kilkudniowego pilotażu (kilka celów pingowanych co 1 s to ciągły, ale niewielki, narzut)
- [ ] realny rozmiar `app.db` po kilku dniach w porównaniu z szacunkiem z `GET /api/quality/retention`
- [ ] zużycie pamięci (RSS kontenera) w trakcie eksportu `probes.csv` dla pełnego, 31-dniowego zakresu
- [ ] czy kopia zapasowa z `docs/operations.md` wykonuje się w obrazie (`sqlite3` albo wariant z `python -c`)

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
- `GET /api/report/quality?from=...&to=...` — starszy raport jakości (JSON)
- `GET /api/quality/report.html?from=...&to=...` — raport do druku/PDF dla dostawcy
- `GET /api/pings?from=...&to=...`
- `GET /api/targets`, `POST /api/targets`, `PUT /api/targets/{id}`, `DELETE /api/targets/{id}` — cele pomiarowe
- `GET /api/quality/status` — stan monitoringu (w tym `raw_range_max_days`)
- `GET /api/quality/retention` — ustawienia retencji + szacowany wzrost bazy
- `GET /api/export/speed.csv?from=...&to=...`
- `GET /api/export/outages.csv?from=...&to=...`
- `GET /api/export/pings.csv?from=...&to=...`
- `GET /api/quality/export/{probes,incidents,aggregates,load-tests}.csv?from=...&to=...`

Daty: ISO-8601, np. `2026-01-28T00:00:00Z`.
