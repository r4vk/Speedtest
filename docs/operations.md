# Operacje — Speedtest (monitoring jakości łącza)

Krótki przewodnik po typowych czynnościach operacyjnych na uruchomionej
instancji: aktualizacja, kopia zapasowa, wycofanie wersji i podstawowe
rozwiązywanie problemów. Dotyczy wersji z modelem danych v2 (monitoring
jakości łącza — patrz `network-quality-design.md` i `NETWORK_QUALITY_PLAN.md`
w tym samym repozytorium).

## Aktualizacja do nowej wersji

```bash
cd Speedtest/app
docker-compose pull
docker-compose up -d
```

Migracja schematu bazy (v1 → v2) jest **automatyczna i addytywna**: tworzy
nowe tabele monitoringu jakości łącza (`probe_targets`, `probe_results`,
`incidents`, `probe_aggregates`, `load_tests`, `diagnostics`, `devices`,
`monitor_sessions`, `config_changes`) i **nie dotyka** istniejących tabel v1
(`speed_tests`, `connectivity_periods`, `connectivity_checks`,
`blocked_periods`, `settings`). Migracja jest opakowana w jedną transakcję —
w razie błędu baza zostaje przy poprzedniej wersji schematu
(`meta.schema_version`), aplikacja nie startuje z połowicznie zmigrowanym
schematem.

Nie jest wymagany żaden ręczny krok poza zwykłym `docker-compose pull &&
docker-compose up -d`, tak jak przy każdej wcześniejszej aktualizacji.

## Kopia zapasowa bazy danych

Baza SQLite (`/data/app.db`) pracuje w trybie WAL, więc kopię można zrobić
**bez zatrzymywania kontenera**:

```bash
docker exec r4vk-speedtest sqlite3 /data/app.db ".backup /data/app-backup.db"
docker cp r4vk-speedtest:/data/app-backup.db ./app-backup-$(date +%Y%m%d).db
```

`.backup` w SQLite korzysta z natywnego API kopii zapasowej (nie z kopiowania
pliku "na żywo"), więc jest bezpieczny nawet przy trwających zapisach sond —
nie trzeba nic zatrzymywać.

Jeśli w obrazie nie ma narzędzia `sqlite3` (starsze wydania go nie
instalowały — `sqlite3: not found`), to samo API wywołuje sam Python, który
jest w obrazie zawsze:

```bash
docker exec r4vk-speedtest python -c "import sqlite3; s=sqlite3.connect('/data/app.db'); d=sqlite3.connect('/data/app-backup.db'); s.backup(d); d.close()"
docker cp r4vk-speedtest:/data/app-backup.db ./app-backup-$(date +%Y%m%d).db
```

Alternatywnie, przy zatrzymanym kontenerze, można skopiować cały katalog
danych (razem z ewentualnymi plikami `-wal`/`-shm`):

```bash
docker-compose stop
docker cp r4vk-speedtest:/data/. ./backup-$(date +%Y%m%d)/
docker-compose start
```

Zalecane: kopia `.backup` przed każdą aktualizacją, plus okresowa kopia (np.
raz dziennie) zaplanowana osobnym cronem/skryptem na hoście — aplikacja sama
tego nie robi.

## Wycofanie wersji (rollback)

Dwie opcje, w kolejności preferencji:

1. **Przywróć kopię zapasową sprzed aktualizacji** i uruchom na niej starą
   wersję obrazu — najpewniejsza opcja, bez niespodzianek.
2. **Uruchom poprzedni obraz (0.0.15 lub starszy) na bieżącej bazie v2** —
   działa, bo migracja v1→v2 jest addytywna: stary kod po prostu nie zna
   nowych tabel i ich nie rusza. Trzeba jednak liczyć się z tym, że:
   - dane monitoringu jakości łącza (sondy ICMP/TCP/DNS/HTTPS, incydenty,
     agregaty, testy obciążeniowe, diagnostyka) **przestają być zbierane i
     widoczne** — stary UI/API ich nie zna,
   - po powrocie do nowszej wersji zbieranie wznawia się automatycznie, ale
     okres pracy na starej wersji zostaje luką w historii jakości łącza,
   - dotychczasowy monitoring "legacy" (ping TCP, test prędkości pobierania,
     tabele `speed_tests`/`connectivity_*`) działa bez zmian w obu wersjach —
     ta historia nie jest niczym zagrożona.

Nie usuwaj tabel v2 ręcznie przy wycofywaniu wersji — nie ma takiej potrzeby
(stary kod je po prostu ignoruje), a to nieodwracalnie kasuje historię
jakości łącza bez żadnej korzyści.

## Rozwiązywanie problemów

### `icmp_method` inny niż `dgram`

`GET /api/quality/status` zwraca pole `icmp_method`:

- `dgram` — OK, nic do zrobienia.
- `raw` — działa, ale wymagał podniesionych uprawnień (kontener ma już
  `NET_RAW` albo działa jako root).
- `ping` — aplikacja spadła do zewnętrznego binarnego `ping`; wolniejsze i
  mniej precyzyjne niż `dgram`/`raw`.
- `unavailable` — ICMP jest całkowicie zablokowane; cele ICMP (`gateway`,
  `cloudflare-dns`, `google-dns`, `quad9-dns`) zaczynają raportować same
  błędy, reszta monitoringu (TCP/DNS/HTTPS) działa bez zmian.

Napraw, odkomentowując w `docker-compose.yml` **jedną** z dwóch opcji (nie
obie naraz):

```yaml
cap_add:
  - NET_RAW
# albo
sysctls:
  - net.ipv4.ping_group_range=0 2147483647
```

po czym `docker-compose up -d`.

**Uwaga:** ta ścieżka nie została zweryfikowana na prawdziwym Synology NAS w
tym wydaniu — traktuj ją jako punkt startowy do diagnozy, nie jako gotową,
sprawdzoną receptę.

### `scheduler.dropped_rows` rośnie

`GET /api/quality/status` → `scheduler.dropped_rows` rośnie, gdy zapis do
SQLite nie nadąża za sondami (przeciążony dysk, baza zablokowana przez inny
proces) i bufor w pamięci przekroczył `PROBE_BUFFER_HARD_MAX` (domyślnie
20 000 wierszy) — wtedy najstarsze, jeszcze niezapisane wiersze są odrzucane.
Sprawdź:

- obciążenie dysku/CPU na NAS-ie w tym okresie,
- `scheduler.flush_errors` w tej samej odpowiedzi oraz logi kontenera —
  powinny wskazywać przyczynę nieudanego zapisu,
- czy liczba aktywnych celów i ich interwały (`PROBE_MAX_CONCURRENCY`,
  interwały poszczególnych celów) nie generują więcej danych, niż dysk NAS-a
  jest w stanie przyjąć.

Odrzucone wiersze są utracone bezpowrotnie (nie ma dla nich retry po fakcie)
— to ostatnia linia obrony przed nieograniczonym wzrostem pamięci procesu,
nie coś, co powinno zdarzać się w normalnej pracy.

### Podejrzenie uszkodzenia bazy

Przy każdym starcie aplikacja uruchamia `PRAGMA quick_check` i loguje
ostrzeżenie, jeśli wynik jest inny niż `ok` (nigdy nie blokuje to startu —
uszkodzona baza to powód do zbadania sprawy, nie do odmowy działania).
Sprawdź logi kontenera przy starcie:

```bash
docker logs r4vk-speedtest 2>&1 | grep -i "integrity"
```

Jeśli pojawi się ostrzeżenie: zrób od razu kopię `.backup` (działa nawet na
częściowo uszkodzonej bazie — kopiuje to, co da się odczytać), a następnie
rozważ przywrócenie z wcześniejszej, znanej dobrej kopii zapasowej.
