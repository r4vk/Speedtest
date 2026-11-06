# NetLog — agent sieci w pasku menu macOS

Natywna aplikacja (SwiftUI, Swift 6), która zastępuje logger powłoki
[`tools/macos-netlog/`](../tools/macos-netlog/). Odpowiada na to samo pytanie: **czy Mac gubi pakiety
na odcinku Wi-Fi, czy problem jest dalej (router/operator)?** Do tego pokazuje stan na żywo w pasku menu.

## Co zbiera

| Co | Jak często | Skąd |
|---|---|---|
| ICMP do routera (wykrywany automatycznie), NAS-a (192.168.10.2) i 1.1.1.1 | co 1 s, próg 1 s | gniazdo ICMP SOCK_DGRAM (bez roota, bez procesu `ping`) |
| Wi-Fi: RSSI, szum, kanał, pasmo, szerokość, tx rate, PHY | co 60 s (popover co 5 s) | CoreWLAN |
| MCS i AWDL | co 60 s | `system_profiler` (CoreWLAN nie podaje MCS; wyłącz: `NETLOG_MCS=0`) i `ifconfig awdl0` |
| Router i adres IP | co 1 s | SystemConfiguration (zamiast `route`/`ipconfig`) |
| Zawieszenie: router milczy ≥ 3 s | na bieżąco | zdarzenie + zrzut diagnostyczny co ≤ 30 s |
| Uśpienie i wybudzenie | na bieżąco | `NSWorkspace.willSleep/didWake` |

### Uśpienie nigdy nie jest awarią

- Przed snem: pomiary stają, otwarte zawieszenie zamyka się z czasem liczonym tylko do uśpienia,
  w dzienniku jest `USYPIANIE`.
- Po wybudzeniu: `WYBUDZENIE`, a potem do 30 s okresu łaski. Wiersze mają wtedy `link=wake` i nie
  otwierają zawieszenia. Okres kończy się przy pierwszej odpowiedzi routera. Jeśli router dalej milczy po
  30 s, zapisuje się to jawnie i dalej działa normalne wykrywanie. Zawieszenie tuż po wybudzeniu też zostaje złapane.
- Luka w pomiarach > 5 s bez powiadomienia o śnie (np. zawieszony proces) to zdarzenie `PRZERWA`, a nie strata.
- Ciemne wybudzenia (Power Nap) nie są mierzone, bo pętla rusza dopiero po pełnym wybudzeniu.

## Gdzie są dane

| Plik | Zawartość |
|---|---|
| `~/Library/Logs/r4vk-netlog/ping-RRRR-MM-DD.csv` | `ts,link,router_ms,nas_ms,internet_ms`, ten sam format co w loggerze; `link` = `up`, `no_ip` lub `wake` (nowe: okres łaski po wybudzeniu) |
| `~/Library/Logs/r4vk-netlog/wifi-RRRR-MM-DD.csv` | `ts,rssi_dbm,noise_dbm,channel,band,width,tx_rate_mbps,mcs,phy,awdl`, ten sam format |
| `~/Library/Logs/r4vk-netlog/events-RRRR-MM-DD.log` | `START`, `ZAWIESZENIE`, `KONIEC`, `ZMIANA`, `ZRZUT`, a nowe: `USYPIANIE`, `WYBUDZENIE`, `PRZERWA`, `BRAK IP`, `STOP` |
| `~/Library/Application Support/NetLog/netlog.sqlite` | te same próby w kształcie `ProbeResult` z serwera, z kolejką wysyłki na NAS (`upload_status`) |

[`summary.sh`](../tools/macos-netlog/summary.sh) działa na tych plikach bez zmian. CSV są usuwane
po 30 dniach. SQLite trzyma 7 dni (3 próby na sekundę to ok. 55 MB na dobę, ok. 380 MB przy 7 dniach).

## Instalacja

Wymagania: macOS 26 lub nowszy i Xcode albo same Command Line Tools (`xcode-select --install`).
`build.sh` sam używa `/Applications/Xcode.app`, jeśli jest (wtedy kompiluje też ikonę aplikacji).

1. Zatrzymaj stary logger, żeby dwa procesy nie pisały do tych samych plików (logi zostają):

   ```bash
   ../tools/macos-netlog/uninstall.sh
   ```

2. Zbuduj i zainstaluj do `/Applications/NetLog.app` (folder Aplikacje), potem uruchom:

   ```bash
   ./build.sh --open
   ```

3. Kliknij ikonę Wi-Fi z NetLog w pasku menu i zaznacz **Przy logowaniu**. To zastępuje LaunchAgent
   loggera (SMAppService). macOS może zapytać o zgodę w Ustawieniach > Ogólne > Rzeczy otwierane przy logowaniu.

**Okno.** Ręczne uruchomienie NetLog (Aplikacje, Spotlight, Launchpad) otwiera okno z tym samym widokiem
co popover. Póki okno jest otwarte, NetLog ma ikonę w Docku. Zamknięcie okna nie kończy pomiarów, a ponowne
uruchomienie aplikacji je otwiera. Start z „Przy logowaniu” okna nie otwiera.

**Nie widać ikony w pasku menu?** Uruchom NetLog z Aplikacji, żeby otworzyć okno, i sprawdź:
- Ustawienia systemowe > Pasek menu > „Zezwalaj w pasku menu”: włącz NetLog;
- na ekranie MacBooka z wcięciem ikony, które się nie mieszczą, chowają się za wcięciem. Zamknij kilka
  innych ikon albo przeciągnij NetLog bliżej zegara (⌘ + przeciągnięcie).

Aktualizacja: `git pull`, potem `./build.sh --open`. Odinstalowanie: odznacz „Przy logowaniu”, zakończ
aplikację, usuń `/Applications/NetLog.app` (folder Aplikacje) (dane zostają w miejscach z tabeli wyżej).

Aplikacja jest podpisana ad hoc (albo certyfikatem „Apple Development”, jeśli jest w pęku kluczy).
Potrzebuje jednej zgody: **Sieć lokalna**. Przy pierwszym uruchomieniu macOS pyta, czy NetLog może łączyć się
z urządzeniami w sieci lokalnej. Kliknij „Zezwól”. Bez tego pingi do NAS-a kończą się błędem „No route to host”
(w CSV puste `nas_ms`), a popover pokazuje czerwone ostrzeżenie. Zgodę zmienia się w Ustawienia systemowe >
Prywatność i ochrona > Sieć lokalna. Przy podpisie ad hoc macOS może zapytać ponownie po każdej przebudowie.
Router i internet działają bez tej zgody. SSID jest ukryty (`<redacted>`), bo wymagałby dostępu do lokalizacji.

### Ustawienia

Okno **Ustawienia** (⌘, albo przycisk „Ustawienia” w oknie i popoverze) zmienia cele pomiarów (NAS, panel
NAS, internet, router albo wykrywanie automatyczne), MCS z system_profiler, czas przechowywania CSV i SQLite
oraz start przy logowaniu. Zmiany działają od następnej sekundy, bez restartu, a w dzienniku zdarzeń zapisuje
się `ZMIANA ustawień: …` (co było, co jest).

To samo z wiersza poleceń (wtedy zakończ i uruchom aplikację ponownie):

```bash
defaults write org.r4vk.netlog nasIP 192.168.10.2
defaults write org.r4vk.netlog nasURL http://192.168.10.2:18000/
defaults write org.r4vk.netlog internetIP 1.1.1.1
defaults write org.r4vk.netlog routerIP 192.168.10.1   # tylko gdy wykrywanie routera się myli
defaults write org.r4vk.netlog mcsFromSystemProfiler 0 # bez system_profiler co minutę
```

Te same nazwy co w netlog.sh działają jako zmienne środowiskowe: `NETLOG_NAS_IP`, `NETLOG_NAS_URL`,
`NETLOG_INTERNET_IP`, `NETLOG_ROUTER_IP`, `NETLOG_DIR`, `NETLOG_KEEP_DAYS`, `NETLOG_STORE_KEEP_DAYS`, `NETLOG_MCS`
(do testów wysyłki także `NETLOG_INGEST_DEVICE`, `NETLOG_INGEST_TOKEN`, `NETLOG_UPLOAD_EVERY`).

## Ikona w pasku menu

| Ikona | Stan |
|---|---|
| Linia EKG (jak systemowe ikony) | wszystko gra |
| Linia EKG, pomarańczowa | w ostatnich 10 s: strata do routera, ≥ 2 straty do NAS-a lub internetu, ≥ 2 razy router > 100 ms albo internet > 200 ms |
| Trójkąt z wykrzyknikiem, czerwony | zawieszenie: router nie odpowiada od ≥ 3 s |
| Przekreślone Wi-Fi, szare | brak adresu IP na Wi-Fi |
| Strzałka odświeżania | po wybudzeniu, Wi-Fi łączy się ponownie |
| Księżyc | uśpienie |

Popover pokazuje: stan, router/NAS/internet (teraz, mediana i strata z 5 minut), wykres RTT z 5 minut
(czerwone kreski = brak odpowiedzi, przerwy w linii = brak pomiaru), stan Wi-Fi i ostatnie zdarzenia.

## Jak czytać wyniki

Tak jak w loggerze. Straty jednocześnie do routera, NAS-a i internetu wskazują odcinek Mac–router (Wi-Fi
lub macOS). Straty tylko do internetu wskazują router albo operatora, więc porównaj z panelem NAS. W zrzucie
`ARP routera … (incomplete)` znaczy, że Mac zgubił łączność L2, choć pokazuje połączenie. Zawieszenia przy
`awdl=active` budzą podejrzenie, że winny jest AWDL (AirDrop/Handoff).

## Dla programisty

```text
Sources/NetLogKit/   pomiary i reguły, bez UI
  ICMP.swift           echo ICMP przez SOCK_DGRAM
  NetInfo.swift        router i IP z SystemConfiguration
  WiFi.swift           CoreWLAN + format wifi-*.csv
  StuckDetector.swift  wykrywanie zawieszeń i stan ikony (Health)
  LogFiles.swift       pliki CSV/log zgodne z netlog.sh
  ResultStore.swift    SQLite: probe_results, wifi_samples, events + kolejka wysyłki
  Diagnostics.swift    zrzut przy zawieszeniu
Sources/NetLog/      aplikacja: Monitor (pętla, sen/wybudzenie), popover, ikona, tryb zrzutu
Tests/NetLogCheck/   sprawdzenia logiki jako program
```

```bash
./build.sh                                                        # build release + instalacja
swift run --scratch-path ~/Library/Developer/NetLog/build NetLogCheck   # sprawdzenia, „PASS NetLogCheck”
tools/snap.sh 20                                                  # 20 s na danych tymczasowych, PNG popovera jasny/ciemny
APP_SIMULATE_SLEEP=6,5 tools/snap.sh 50                           # z symulacją uśpienia (te same powiadomienia NSWorkspace)
NETLOG_ROUTER_IP=192.0.2.1 tools/snap.sh 20                       # „milczący router” (adres dokumentacyjny) — test zawieszenia bez psucia sieci
```

`tools/snap.sh` nigdy nie dotyka prawdziwych logów ani bazy: używa `NETLOG_DIR` i `APP_STORE` w `$TMPDIR/NetLog-snap`.

Bez pełnego Xcode nie działają makra `@State` (SwiftUI) i `@Test` (Swift Testing), a XCTest nie istnieje.
Dlatego kod nie używa `@State`, a testy są zwykłym programem. Ikona aplikacji (`Icon/`) kompiluje się tylko
z Xcode (`actool`); bez niego `build.sh` ją pomija, a aplikacja paska menu i tak nie ma ikony w Docku.

## Wysyłka na NAS (etap 9)

NetLog wysyła swoje próby do `POST {Panel NAS}api/ingest/results` (docs/network-quality-design.md §15),
żeby panel Speedtest mógł porównać Wi-Fi Maca z kablem NAS-a na wspólnej osi czasu.

**Włączenie:** w panelu Speedtest otwórz Ustawienia → Urządzenia wysyłające wyniki i dodaj urządzenie. Panel
pokaże identyfikator (np. `macbook-rafala`) i token, który widać tylko raz. W NetLog w Ustawienia → Wysyłka
na NAS wpisz identyfikator i wklej token, potem kliknij „Zapisz”. Token trafia do pęku kluczy (wpis „NetLog —
token wysyłki wyników na NAS”), a nie do `defaults`. NetLog nie rejestruje się sam. Wymaga Speedtest z API
wysyłki: starsza wersja odpowiada 404 i NetLog pokazuje, że trzeba ją zaktualizować.

**Co idzie, a co zostaje na Macu:**
- idą próby do routera (`gateway`) i internetu (`internet`, na NAS-ie to `cloudflare-dns`);
- zostają na Macu próby do samego NAS-a (NAS nie ma celu „siebie”, serwer odrzuciłby je jako `unknown_target`)
  i próby z okresu łaski po wybudzeniu (`link=wake`). Serwer nie zna kolumny `link`, więc policzyłby
  te timeouty jako straty, a sen nie jest awarią.

**Rytm i błędy:**
- paczka do 1000 wyników co 60 s, przy zaległości następna po 5 s, od razu po wybudzeniu i powrocie sieci;
- przyjęte, duplikaty i odrzucone wychodzą z kolejki (`upload_status` = `sent` albo `rejected:<powód>`),
  bo odrzucony wiersz byłby odrzucany przy każdej próbie;
- 401 (zły lub unieważniony token) albo 403 (identyfikator nie pasuje do tokenu) zatrzymują wysyłkę,
  a popover i Ustawienia pokazują to na czerwono. Po poprawce: „Zapisz” albo „Wyślij teraz”;
- 413 zmniejsza paczkę o połowę. Przy 5xx, braku sieci i timeoucie odstęp rośnie 60 s → 2 → 4 → 8 → 15 min,
  a kolejka zostaje (SQLite trzyma 7 dni);
- `external_id = "<epoch_ms>:<cel>"`, więc powtórka po zerwanym połączeniu daje duplikaty, a nie podwójne wiersze.

Popover pokazuje stan wysyłki, czas ostatniej i długość kolejki. Ustawienia pokazują też „Wyślij teraz”.
Zdarzenia `WYSYŁKA …` trafiają do dziennika tylko przy zmianie stanu (pierwszy sukces, błąd, 401/403).

**Test na lokalnym serwerze** (z katalogu `Speedtest/`, NAS-a nie dotyka):

```bash
cd app && DATA_DIR=/tmp/st-dev ../.venv/bin/uvicorn speedtest_app.main:app --port 8765
curl -X POST localhost:8765/api/devices -H 'Content-Type: application/json' -d '{"name":"NetLog test"}'
NETLOG_NAS_URL=http://localhost:8765/ NETLOG_INGEST_DEVICE=netlog-test NETLOG_INGEST_TOKEN=<token> \
  NETLOG_UPLOAD_EVERY=5 tools/snap.sh 25
```

Zmienne `NETLOG_INGEST_DEVICE`, `NETLOG_INGEST_TOKEN` i `NETLOG_UPLOAD_EVERY` służą tylko do testów:
nadpisują ustawienia i pęk kluczy.

Do zrobienia: zdarzenia snu i przerw (`events`) nie idą jeszcze na NAS, bo serwer nie ma na nie miejsca.
