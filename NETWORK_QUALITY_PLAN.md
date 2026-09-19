# Plan realizacji monitoringu jakości internetu

Data: 2026-09-19. Status: etapy 1–8 zaimplementowane na poziomie kodu i testów (wydanie 0.1.0);
weryfikacja na NAS-ie i pilotaż nieprzeprowadzone; etap 9 nierozpoczęty. Projekt techniczny:
[docs/network-quality-design.md](docs/network-quality-design.md), eksploatacja: [docs/operations.md](docs/operations.md).

## Cel i zakres

Rozbudować istniejącą aplikację na NAS-ie tak, aby dokumentowała utratę odpowiedzi na sondy, opóźnienia, krótkie zakłócenia i degradację usług oraz generowała raport dla dostawcy internetu. Zachować FastAPI, SQLite, panel WWW i istniejącą historię.

Priorytetem jest monitoring z NAS-a po kablu. Agent macOS jest ostatnim, oddzielnym etapem. Sam panel otwarty na Macu nie mierzy jego Wi-Fi. Wyniki NAS-a dotyczą drogi NAS–router–internet i nie stanowią samodzielnego dowodu winy operatora.

## Instrukcja dla agenta realizującego

1. Przeczytaj `AGENTS.md`, ten plan i aktualny kod. Sprawdź status Git i lokalne instrukcje; zachowaj cudze zmiany.
2. Realizuj etapy w podanej kolejności, z wyjątkiem podstawowego panelu i eksportu z etapu 7, które należą do pierwszej użytecznej wersji po etapach 1–3.
3. Przed zmianą danych zaprojektuj kompatybilną migrację. Nie usuwaj istniejącej historii ani nie reinterpretuj danych TCP jako utraty pakietów.
4. Wybory implementacyjne zapisuj w sekcji „Dziennik realizacji”. Parametry opisane jako propozycje nie są ustalonymi wymaganiami — dobierz je i uzasadnij.
5. Dodawaj testy dla obliczeń, harmonogramowania, migracji i obsługi błędów. Nie wykonuj symulacji awarii na aktywnym łączu użytkownika.
6. Po każdym etapie odnotuj zmienione pliki, wykonane testy, wynik i pozostające ograniczenia. Zaznacz etap jako ukończony dopiero po spełnieniu jego kryteriów.
7. Brak serwera UDP nie blokuje etapów niezależnych. Nie kupuj ani nie twórz zewnętrznej infrastruktury bez odpowiedniego zlecenia.
8. Ten dokument jest planem prac, nie poleceniem natychmiastowego wdrożenia na NAS, publikacji obrazu czy wysyłania raportu operatorowi. Przy realizacji kieruj się zakresem aktualnego zlecenia użytkownika.

## Punkty wejścia w istniejącym kodzie

- `app/speedtest_app/connectivity.py`: obecny test zestawienia połączenia TCP.
- `app/speedtest_app/scheduler.py`: pętle monitoringu, buforowanie i testy prędkości.
- `app/speedtest_app/db.py`: schemat i operacje SQLite.
- `app/speedtest_app/main.py`: API, ustawienia, raport jakości i eksporty.
- `app/speedtest_app/network_tools.py`: diagnostyka na żądanie, MTR i iperf3; obecna integracja iperf3 mierzy TCP.
- `app/static/app.js`, `app/static/index.html`, `app/static/styles.css`: panel.
- `app/Dockerfile`, `app/docker-compose.yml`: obraz i uruchomienie na NAS. Zweryfikuj rzeczywiste ścieżki; obecny Compose znajduje się w `app/`.

## Zasady wiarygodności pomiarów

- Rozróżniaj dostępność, pogorszoną jakość i brak danych. Nie uznawaj niewykonanej próby za utracony pakiet ani za sukces.
- Strata odpowiedzi ICMP, nieudane połączenie TCP i utrata datagramów UDP to różne metryki. Nie łącz ich w jeden procent strat.
- Wynik dotyczy konkretnego urządzenia, celu, protokołu, kierunku i konfiguracji. Zachowuj te informacje z pomiarem.
- Timeout sondy oznacza brak odpowiedzi w zadanym czasie; jawnie zapisuj próg. Pomiar w obie strony nie lokalizuje samodzielnie kierunku straty.
- Próbkowanie co sekundę nie gwarantuje wykrycia wszystkich krótszych zakłóceń. Nie przedstawiaj szacowanych granic incydentu jako dokładnego czasu fizycznej awarii.
- Brak odpowiedzi routera pośredniego w MTR nie dowodzi utraty ruchu przekazywanego dalej.
- Brak pomiarów po restarcie, wyłączeniu monitora lub błędzie procesu musi być widoczny w raporcie i pokryciu danych.
- Czas zdarzeń zapisuj w UTC, pokazuj strefę w raportach; czas trwania prób i harmonogramowanie opieraj na zegarze monotonicznym tam, gdzie to właściwe.
- Monitoring ma być lekki. Testy generujące obciążenie oznaczaj i oddzielaj od pomiarów podczas normalnego użytkowania.

## Etap 1 — model danych i dostępność

- [x] Wprowadź osobne stany dostępności, jakości i braku danych. (`availability.py`: `up|down|no_data`, `ok|degraded|unknown`)
- [x] Zaprojektuj zapis urządzenia pomiarowego (na razie NAS), celu, protokołu, rozwiązanego IP/rodziny adresów, czasów próby, wyniku, opóźnienia i rodzaju błędu. (tabela `probe_results`, `probe_types.py`)
- [x] Rejestruj sesje monitora, przerwy, wyłączenia harmonogramem i zmiany konfiguracji. (`monitor_sessions`, `blocked_periods`, `config_changes`)
- [x] Zapewnij migrację istniejącej bazy bez utraty danych. Historię TCP oznacz jako dotychczasowe pomiary TCP. (schemat v2, migracja w jednej transakcji, `legacy_tcp` bez procentu strat)
- [x] Popraw raport: uwzględniaj rzeczywisty okres obserwacji i podawaj pokrycie pomiarami; nie rozciągaj ostatniego stanu przez nieobserwowane okresy. (`coverage.py`, `/api/report/quality` przycina do okresów obserwowanych)

Kryterium ukończenia: testy migracji i raportu potwierdzają poprawne zachowanie dla pustej bazy, starej historii, restartu, wyłączenia monitora i zakresu częściowo bez danych.

## Etap 2 — równoległe sondy z NAS-a

- [x] Dodaj ICMP do jawnie skonfigurowanej bramy sieci domowej i kilku konfigurowalnych celów internetowych w różnych sieciach. (cele `gateway` — wyłączony do czasu ustawienia `GATEWAY_HOST`/`gateway_host`, `1.1.1.1`, `8.8.8.8`, `9.9.9.9`)
- [x] Zachowaj TCP jako oddzielny pomiar. (`tcp_probe.py`, cel `legacy-tcp`)
- [x] Przyjmij początkowo ICMP co 1 s, z ustawieniami per cel. Gęstsze próbkowanie zostaw do ograniczonego trybu diagnostycznego. (`interval_seconds` ≥ 1 s, `diagnostic_mode` pozwala ≥ 0,2 s)
- [x] Zapisuj każdą wykonaną próbę, timeout i błędy uruchomienia sondy, rozróżniając ich znaczenie. (`outcome` ok/timeout/error + `error_kind`)
- [x] Zadbaj o niezależność celów, ograniczoną współbieżność i brak narastającej kolejki przy timeoutach. (`probe_scheduler.py`: pętla per cel, semafor, pomijanie spóźnionych ticków)
- [ ] Zweryfikuj ICMP oraz minimalne wymagane uprawnienia kontenera na Synology. Nie zakładaj, że domyślna brama kontenera jest routerem domowym. (kod: drabinka dgram → raw → `ping` z widocznym `icmp_method`; **nie zweryfikowano na rzeczywistym NAS-ie** — patrz README, sekcja "Weryfikacja na NAS-ie")

Kryterium ukończenia: timeout lub błąd jednego celu nie blokuje innych; sondy i błędy są poprawnie zapisane, a konfiguracja działa na docelowym NAS-ie.

## Etap 3 — statystyki i incydenty

- [x] Obliczaj per cel/protokół liczbę prób i odpowiedzi, timeouty, procent utraconych odpowiedzi, medianę, p95, p99, maksimum RTT i najdłuższą serię niepowodzeń. (`stats.py`)
- [x] Zdefiniuj i opisz zmienność RTT; nie nazywaj jej bez wyjaśnienia jitterem UDP. (`rtt_variation_ms` = średnia |Δ| kolejnych RTT, `rtt_spread_ms` = p95−p50; "jitter" tylko dla UDP iperf3)
- [x] Dodaj krótkie okna analizy (propozycja: 10 s i 1 min) oraz agregaty godzinowe i dobowe. (okna 10 s dla incydentów, bucket dowolny ≥ 10 s na osi czasu, `probe_aggregates` 1h/1d)
- [x] Procent strat agreguj z liczników, nie przez nieważoną średnią procentów. Percentyli nie wyliczaj jako średniej percentyli podokresów. (`merge_counters`, agregaty dobowe z surowych wierszy)
- [x] Dodaj konfigurowalne progi degradacji, okres stabilizacji przy zamykaniu incydentu i obsługę zbyt małej liczby próbek. (ustawienia `incident_*`)
- [x] Zachowuj przebieg i granice incydentu z informacją o rozdzielczości pomiaru. (`incidents.summary_json`, `window_seconds`, `probe_interval_seconds`)

Kryterium ukończenia: deterministyczne testy wykrywają krótką serię strat i skoki RTT, nie ukrywają ich w średniej dobowej oraz prawidłowo obsługują przerwy danych.

## Etap 4 — DNS, TCP i HTTPS

- [x] Dodaj lekkie, okresowe sondy DNS i HTTPS do konfigurowalnych endpointów. (`dns_probe.py`, `https_probe.py`; cele `dns-system`, `https-cloudflare`, `https-google`)
- [x] Zapisuj kategorie błędów DNS, TCP, TLS i HTTP; rozdzielaj czasy etapów tam, gdzie metoda pomiaru to umożliwia. (`error_kind`, `stages_json`: dns/connect/tls/ttfb)
- [x] Rozróżniaj problem konkretnej usługi od ogólnej niedostępności sieci. (dostępność liczona tylko z celów internetowych/TCP; incydenty DNS/HTTPS raportowane osobno)
- [x] Zapisuj użyty adres IP; analizuj IPv4 i IPv6 oddzielnie, jeśli są dostępne. (`resolved_ip`, `ip_family`, `family_pref` per cel)

Kryterium ukończenia: kontrolowane błędy DNS, TLS i HTTP są rozpoznawane osobno; sukces TCP nie jest przedstawiany jako sukces HTTPS.

## Etap 5 — UDP i pomiary pod obciążeniem

Zależność: dostępny, kontrolowany lub uzgodniony serwer iperf3 poza domem. Nie wysyłaj testów UDP do przypadkowych publicznych usług. Sam brak konfiguracji serwera oznacza funkcję nieskonfigurowaną, nie awarię internetu.

- [x] Rozbuduj iperf3 o UDP, upload i download oraz ustawienia przepływności, długości testu, rozmiaru datagramów i harmonogramu. (`iperf_udp.py`, `load_tests.py`, ustawienia `load_test_*`)
- [x] Zapisuj statystyki odbiorcy: odebrane/utracone datagramy, procent strat i jitter; zachowuj wyniki cząstkowe i surowy JSON. (`load_tests.result_json/raw_json`)
- [x] Waliduj strukturę i błędy wyniku iperf3. Brak danych lub błąd narzędzia nie może dawać pozornych 0% strat. (`parse_result` → `ResultValidationError`; testy na fixture'ach)
- [x] Ogranicz generowane obciążenie; zapobiegaj nakładaniu testów UDP i testów prędkości. (wspólna blokada z testem prędkości, domyślnie wyłączone, brak serwera = `skipped/not_configured`)
- [x] Utrzymuj sondy opóźnienia podczas downloadu/uploadu i porównuj je z okresem bez testowego obciążenia. (`load_test_id` na wierszach sond, tabela "opóźnienie pod obciążeniem" w raporcie)
- [x] Oznaczaj testy obciążeniowe na osi czasu oraz w raportach. (oś czasu API/panel, raport)
  Uwaga: pomiar z rzeczywistym serwerem iperf3 nie został wykonany (brak uzgodnionego serwera).

Kryterium ukończenia: poprawny pomiar strat w obu kierunkach w kontrolowanym środowisku i rozróżnienie wyników zwykłego monitoringu od testów obciążeniowych.

## Etap 6 — diagnostyka incydentów

- [x] Uruchamiaj MTR po wykryciu utrzymującej się degradacji do dotkniętych celów. (`diagnostics.py`, wyzwalane zdarzeniami incydentów, poza pętlą incydentów)
- [x] Zachowuj wynik, surowy materiał, czas wykonania i błędy diagnostyki. (tabela `diagnostics`)
- [x] Powiąż incydent z wynikami routera, innych celów i aktywnych testów obciążeniowych. (`GET /api/quality/incidents/{id}`: `related_targets`, `load_tests`, `diagnostics`)
- [x] Wprowadź limit częstotliwości i współbieżności diagnostyki. (`diagnostics_*`; odmowy zapisywane jako `rate_limited`, ponawiane po `min_interval`)
- [x] Prezentuj hipotezy lokalizacji problemu jako interpretacje, bez automatycznego przypisania winy operatorowi. ("Możliwa przyczyna: …")

Kryterium ukończenia: incydent posiada powiązane pomiary i diagnostykę, a długotrwała degradacja nie wywołuje lawiny procesów MTR.

## Etap 7 — panel i raport dla dostawcy

Podstawowy zakres tego etapu realizuj już po etapie 3; kolejne metryki udostępniaj wraz z implementacją etapów 4–6.

- [x] Dodaj osobny status dostępności i jakości oraz podpis „pomiar z NAS-a po kablu”. (panel: pasek statusu, `static/quality.js`)
- [x] Dodaj wykresy strat i opóźnień per cel, wspólną oś incydentów, przerw i testów obciążeniowych. (panel “Jakość łącza”)
- [x] Dodaj szczegóły incydentu i ręczne oznaczenie objawu, np. „zacięcie TV”; oznaczenie użytkownika odróżniaj od pomiaru. (tabela `annotations`, odznaka “zgłoszenie użytkownika”)
- [x] Przygotuj raport HTML do druku/PDF za wybrany okres oraz CSV z danymi źródłowymi. (`/api/quality/report.html`, `/api/quality/export/*.csv`)
- [x] Raport ma zawierać: czas i strefę, pokrycie danych, urządzenie/interfejs, metodę, konfigurację, cele, wersję aplikacji, liczniki, statystyki, incydenty, wykresy i ograniczenia. (`report.py`, `templates/report.html`)
- [x] Rozdzielaj ICMP/TCP/UDP oraz kierunki UDP. Nie wyliczaj utraty pakietów ze starej historii TCP. (osobne tabele; `legacy_tcp` tylko liczniki)
- [x] Zapewnij zgodność liczb w panelu, API, CSV i raporcie, również dla granic zakresu czasu. (wspólne funkcje `quality_views.py`; test spójności `test_exports.py`)

Kryterium ukończenia: raport jest czytelny bez ręcznego składania zrzutów ekranu, a jego liczby można odtworzyć z danych źródłowych. Brak danych jest widoczny.

## Etap 8 — retencja, weryfikacja i wdrożenie NAS

Podstawowe testy wykonuj na bieżąco; ten etap zamyka weryfikację całości.

- [x] Dobierz konfigurowalną retencję: krótszą dla sond, dłuższą dla agregatów i incydentów; oszacuj przyrost bazy na dobę. (`retention.py`, `retention_*`, `GET /api/quality/retention`)
- [x] Zdefiniuj dostępność surowych danych w raportach po upływie retencji; nie sugeruj pełnego eksportu, jeśli pozostały tylko agregaty. (`data_source`, `covered_from/to`, komentarz w CSV, nota w raporcie)
- [x] Zapewnij ograniczone buforowanie i obsługę błędów zapisu SQLite; określ możliwą utratę bufora przy nagłym zatrzymaniu. (bufor schedulera z limitem twardym, `busy_timeout`, `quick_check`; utrata ≤ `PROBE_FLUSH_SECONDS` + wiersze zatrzymane przy nieudanym zapisie)
- [x] W odizolowanym środowisku zasymuluj straty, skoki opóźnienia, brak DNS, awarię jednego celu i restart monitora. (deterministyczne testy z podstawionymi sondami i zegarami; bez symulacji na aktywnym łączu)
- [ ] Zweryfikuj migrację starej bazy, raportowanie i zużycie zasobów. (migracja i raportowanie: testy automatyczne; **zużycie zasobów na NAS-ie niezweryfikowane**)
- [x] Przygotuj instrukcję konfiguracji, aktualizacji, kopii bazy i wycofania wersji z uwzględnieniem migracji. (`app/README.md`, `docs/operations.md`)
- [ ] W ramach osobno zleconego wdrożenia przeprowadź kilkudniowy pilotaż na NAS-ie, zapisując obciążenie CPU, RAM, dysku i stabilność zbierania danych. (**nie wykonano — wymaga osobnego zlecenia**)

Kryterium ukończenia: kontrolowane zakłócenia są wykrywane, dane pozostają spójne, a pilotaż potwierdza stabilność bez nadmiernego obciążania NAS-a i łącza. Brak dostępu do NAS-a oznacz jako niewykonaną weryfikację, nie sukces.

## Etap 9 — agent macOS, dopiero po części NAS

- [ ] Dodaj pomiary Mac → NAS i Mac → te same cele internetowe.
- [ ] Obsłuż uśpienie, wybudzenie, zmianę interfejsu i lokalne buforowanie przy braku dostępu do NAS-a.
- [ ] Dodaj uwierzytelnione przesyłanie wyników, identyfikację urządzenia i idempotentny import.
- [ ] Porównuj wyniki Maca i NAS-a na wspólnej osi czasu, uwzględniając synchronizację zegarów.

Kryterium ukończenia: możliwe jest porównanie jednoczesnych pomiarów kablowych i Wi-Fi, a uśpienie Maca nie jest klasyfikowane jako awaria internetu.

## Kolejność wydań

1. Pierwsza użyteczna wersja NAS: etapy 1–3 + podstawowy panel i eksport z etapu 7.
2. Rozszerzona diagnostyka NAS: etapy 4–6 + pełny raport z etapu 7.
3. Stabilizacja i pilotaż: etap 8.
4. Rozszerzenie o Wi-Fi Maca: etap 9.

## Dane potrzebne przy konfiguracji NAS

Nie są wymagane do rozpoczęcia implementacji, ale są potrzebne do jej weryfikacji w docelowej sieci:

- adres bramy sieci domowej i sposób połączenia NAS-a;
- model/wersja DSM i sposób uruchamiania kontenera;
- lista uzgodnionych celów pomiarowych i dostępność IPv6;
- docelowy serwer UDP, jeśli etap 5 ma być uruchomiony;
- parametry łącza do doboru limitów testów oraz dostępny budżet dyskowy.

## Dziennik realizacji

| Data | Etap | Zmiany i decyzje | Weryfikacja | Ograniczenia / następny krok |
|------|------|-----------------|-------------|----------------------------|
| 2026-09-19 | Plan | Zapisano plan; NAS jest priorytetem, macOS na końcu | Analiza istniejącego kodu w rozmowie | Implementacja i pomiary sieci nie zostały rozpoczęte |
| 2026-09-19 | Projekt | Wiążący projekt techniczny `docs/network-quality-design.md` (schemat v2, sondy, statystyki, maszyna stanów incydentów, dostępność, API, raport, retencja). Parametry-propozycje ustalone jako domyślne i konfigurowalne: ICMP 1 s/1000 ms, okno 10 s, progi 20 % strat / p95 150 ms / seria 3, otwarcie po 2 oknach, stabilizacja 60 s, retencja surowych 14 d | Przegląd spójności planu vs projekt | Docker niedostępny lokalnie — obraz nie zbudowany w tej sesji |
| 2026-09-19 | 1 | `db.py` schemat v2 (migracja 1→2 w jednej transakcji, seed celów), `probe_types.py`, `quality_db.py`, `coverage.py` (sesje monitora, pokrycie), `config_changes`; `/api/report/quality` liczy przestój tylko w czasie obserwowanym; znaczniki czasu zawsze z milisekundami (`to_iso_z`) | pytest: migracja pustej i starej bazy, idempotencja, rollback, pokrycie, raport przy restarcie/wyłączeniu | — |
| 2026-09-19 | 2 | `icmp_probe.py` (gniazdo dgram → raw → `ping`, metoda widoczna w statusie; `ping` exit 2 = błąd na Linuksie, brak odpowiedzi na BSD), `tcp_probe.py`, `probe_scheduler.py` (pętla per cel, semafor, pomijanie spóźnionych ticków, bufor z limitem twardym i backoffem) | pytest z podstawionymi gniazdami/zegarami; ICMP dgram działa na macOS deweloperskim | Uprawnienia ICMP na Synology niezweryfikowane |
| 2026-09-19 | 3 | `stats.py` (straty z liczników, percentyle nearest-rank, zmienność RTT), `incidents.py` (maszyna stanów pending/open/closed, zamknięcia recovered/no_data/shutdown), `aggregates.py` (1h/1d z surowych wierszy) | testy deterministyczne: krótka seria strat wykrywana mimo niskiej średniej dobowej; p95 dobowe ≠ średnia p95 godzinowych | — |
| 2026-09-19 | 4 | `dns_probe.py`, `https_probe.py` (etapy dns/connect/tls/ttfb, kategorie błędów; sukces TCP ≠ sukces HTTPS) | testy z lokalnymi serwerami i certyfikatem self-signed | — |
| 2026-09-19 | 5 | `iperf_udp.py` (argv, walidacja JSON, straty z liczników odbiorcy), `load_tests.py` (harmonogram, blokada z testem prędkości, `load_test_id` na sondach), porównanie opóźnienia pod obciążeniem w raporcie | testy na fixture'ach iperf3 3.12 | Brak serwera iperf3 — pomiar rzeczywisty niewykonany; domyślnie wyłączone |
| 2026-09-19 | 2–3 integracja | `availability.py` (up/down/no_data z celów internetowych/TCP; `no_data` kończy okres, znacznik awarii utrzymany do `incident_no_data_close_seconds`), `quality_engine.py` (pętle: dostępność, incydenty, agregaty, odświeżanie ustawień), `main.py` na `lifespan`; stara pętla TCP usunięta | testy silnika z podstawionym rejestrem sond i zegarem; `/api/status` zachowuje kształt | — |
| 2026-09-19 | 6 | `diagnostics.py` (MTR po otwarciu incydentu, poza pętlą incydentów; limity współbieżności/częstotliwości/na incydent; odmowy zapisane jako `rate_limited` i ponawiane; hipotezy „Możliwa przyczyna: …") | testy z podstawionym procesem mtr | — |
| 2026-09-19 | 7 | `api_quality.py` + `api_quality_exports.py` + `quality_views.py` (wspólne funkcje dla API/CSV/raportu), `report.py` + szablon (HTML samowystarczalny, A4, SVG), `quality_settings.py` (33 klucze), panel `static/quality.js` (status, wykres per cel ze wspólną osią, tabela statystyk, incydenty, objawy użytkownika, eksporty, ustawienia celów/progów/testów/retencji) | test spójności liczb stats/oś czasu/CSV/raport na granicach zakresu; test kontraktu id HTML↔JS; `node --check` | Panel bez testów przeglądarkowych automatycznych (kontrola ręczna w przeglądarce w tej sesji) |
| 2026-09-19 | 8 | `retention.py` (agregaty przed usunięciem, granica surowych wyrównana do doby UTC, partie po 5000), `db.integrity_quick_check`, `busy_timeout`, `GET /api/quality/retention`, `app/README.md` sekcja monitoringu, `docs/operations.md` | testy retencji (kolejność, partie, drugie uruchomienie bez zmian) | Pilotaż na NAS-ie, zużycie zasobów i uprawnienia ICMP na Synology — do wykonania w osobnym zleceniu |
| 2026-09-19 | 9 | Nierozpoczęty (zgodnie z kolejnością wydań: po pilotażu NAS). Brief zadania przygotowany (ingest API z tokenami urządzeń, idempotentny import po `external_id`, agent macOS) | — | Wymaga wydania 4 |
