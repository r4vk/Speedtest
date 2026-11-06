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
| `~/Library/Application Support/NetLog/netlog.sqlite` | te same próby w kształcie `ProbeResult` z serwera, z kolejką do wysłania na NAS (etap 9) |

[`summary.sh`](../tools/macos-netlog/summary.sh) działa na tych plikach bez zmian. CSV są usuwane
po 30 dniach. SQLite trzyma 7 dni (3 próby na sekundę to ok. 55 MB na dobę, ok. 380 MB przy 7 dniach).

## Instalacja

Wymagania: macOS 26 lub nowszy i Command Line Tools (`xcode-select --install`). Pełne Xcode nie jest potrzebne.

1. Zatrzymaj stary logger, żeby dwa procesy nie pisały do tych samych plików (logi zostają):

   ```bash
   ../tools/macos-netlog/uninstall.sh
   ```

2. Zbuduj i zainstaluj do `~/Applications/NetLog.app`, potem uruchom:

   ```bash
   ./build.sh --open
   ```

3. Kliknij ikonę Wi-Fi z NetLog w pasku menu i zaznacz **Przy logowaniu**. To zastępuje LaunchAgent
   loggera (SMAppService). macOS może zapytać o zgodę w Ustawieniach > Ogólne > Rzeczy otwierane przy logowaniu.

Aktualizacja: `git pull`, potem `./build.sh --open`. Odinstalowanie: odznacz „Przy logowaniu”, zakończ
aplikację, usuń `~/Applications/NetLog.app` (dane zostają w miejscach z tabeli wyżej).

Aplikacja jest podpisana ad hoc (albo certyfikatem „Apple Development”, jeśli jest w pęku kluczy).
Potrzebuje jednej zgody: **Sieć lokalna**. Przy pierwszym uruchomieniu macOS pyta, czy NetLog może łączyć się
z urządzeniami w sieci lokalnej. Kliknij „Zezwól”. Bez tego pingi do NAS-a kończą się błędem „No route to host”
(w CSV puste `nas_ms`), a popover pokazuje czerwone ostrzeżenie. Zgodę zmienia się w Ustawienia systemowe >
Prywatność i ochrona > Sieć lokalna. Przy podpisie ad hoc macOS może zapytać ponownie po każdej przebudowie.
Router i internet działają bez tej zgody. SSID jest ukryty (`<redacted>`), bo wymagałby dostępu do lokalizacji.

### Ustawienia

Bez okna ustawień. Cele zmienia się tak (potem zakończ i uruchom aplikację ponownie):

```bash
defaults write org.r4vk.netlog nasIP 192.168.10.2
defaults write org.r4vk.netlog nasURL http://192.168.10.2:18000/
defaults write org.r4vk.netlog internetIP 1.1.1.1
defaults write org.r4vk.netlog routerIP 192.168.10.1   # tylko gdy wykrywanie routera się myli
defaults write org.r4vk.netlog mcsFromSystemProfiler 0 # bez system_profiler co minutę
```

Te same nazwy co w netlog.sh działają jako zmienne środowiskowe: `NETLOG_NAS_IP`, `NETLOG_NAS_URL`,
`NETLOG_INTERNET_IP`, `NETLOG_ROUTER_IP`, `NETLOG_DIR`, `NETLOG_KEEP_DAYS`, `NETLOG_STORE_KEEP_DAYS`, `NETLOG_MCS`.

## Ikona w pasku menu

| Ikona | Stan |
|---|---|
| Wi-Fi (jak systemowe) | wszystko gra |
| Wi-Fi z wykrzyknikiem, pomarańczowa | w ostatnich 10 s: strata do routera, ≥ 2 straty do NAS-a lub internetu, ≥ 2 razy router > 100 ms albo internet > 200 ms |
| Przekreślone Wi-Fi, czerwone | zawieszenie: router nie odpowiada od ≥ 3 s |
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

## Etap 9 — wysyłka na NAS (jeszcze niezrobiona)

Wszystko jest przygotowane pod `POST /api/ingest/results` z [docs/network-quality-design.md](../docs/network-quality-design.md) §15:

- `probe_results` w SQLite ma pola `ProbeResult` serwera (`protocol`, `started_at` UTC z ms i `Z`,
  `duration_ms`, `outcome` ok/timeout/error, `rtt_ms`, `timeout_ms`, `resolved_ip`, `ip_family`,
  `error_kind`, `error_detail`). Do tego `target` (dopasowanie po nazwie: `gateway`, `nas`, `internet`),
  `link` oraz `uploaded_at`.
- `external_id = "<epoch_ms>:<target>"` jest deterministyczny, więc ponowna wysyłka po zerwanym połączeniu
  trafi w unikalny indeks `(device_id, external_id)` jako `duplicates`.
- `ResultStore.unsent(limit:)` i `markUploaded(rowIDs:)` to kolejka dla wysyłającego. Brak NAS-a nie
  gubi danych przez 7 dni.
- `events` zawiera okresy snu (`sleep`/`wake`), przerwy (`gap`) i `start`/`stop`, czyli pokrycie pomiarami,
  żeby serwer odróżnił „Mac spał” od „brak łączności”.
- Brakuje jeszcze: rejestracji urządzenia (`POST /api/devices`, token w pęku kluczy), wysyłki partiami
  z `sent_at` (serwer liczy z niego przesunięcie zegara) i celów `nas` na serwerze, którego `kind`
  dziś nie przewiduje.
