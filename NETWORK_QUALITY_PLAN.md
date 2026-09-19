# Plan realizacji monitoringu jakości internetu

Data: 2026-09-19. Status: plan zaakceptowanego kierunku; implementacja nie rozpoczęta.

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

- [ ] Wprowadź osobne stany dostępności, jakości i braku danych.
- [ ] Zaprojektuj zapis urządzenia pomiarowego (na razie NAS), celu, protokołu, rozwiązanego IP/rodziny adresów, czasów próby, wyniku, opóźnienia i rodzaju błędu.
- [ ] Rejestruj sesje monitora, przerwy, wyłączenia harmonogramem i zmiany konfiguracji.
- [ ] Zapewnij migrację istniejącej bazy bez utraty danych. Historię TCP oznacz jako dotychczasowe pomiary TCP.
- [ ] Popraw raport: uwzględniaj rzeczywisty okres obserwacji i podawaj pokrycie pomiarami; nie rozciągaj ostatniego stanu przez nieobserwowane okresy.

Kryterium ukończenia: testy migracji i raportu potwierdzają poprawne zachowanie dla pustej bazy, starej historii, restartu, wyłączenia monitora i zakresu częściowo bez danych.

## Etap 2 — równoległe sondy z NAS-a

- [ ] Dodaj ICMP do jawnie skonfigurowanej bramy sieci domowej i kilku konfigurowalnych celów internetowych w różnych sieciach.
- [ ] Zachowaj TCP jako oddzielny pomiar.
- [ ] Przyjmij początkowo ICMP co 1 s, z ustawieniami per cel. Gęstsze próbkowanie zostaw do ograniczonego trybu diagnostycznego.
- [ ] Zapisuj każdą wykonaną próbę, timeout i błędy uruchomienia sondy, rozróżniając ich znaczenie.
- [ ] Zadbaj o niezależność celów, ograniczoną współbieżność i brak narastającej kolejki przy timeoutach.
- [ ] Zweryfikuj ICMP oraz minimalne wymagane uprawnienia kontenera na Synology. Nie zakładaj, że domyślna brama kontenera jest routerem domowym.

Kryterium ukończenia: timeout lub błąd jednego celu nie blokuje innych; sondy i błędy są poprawnie zapisane, a konfiguracja działa na docelowym NAS-ie.

## Etap 3 — statystyki i incydenty

- [ ] Obliczaj per cel/protokół liczbę prób i odpowiedzi, timeouty, procent utraconych odpowiedzi, medianę, p95, p99, maksimum RTT i najdłuższą serię niepowodzeń.
- [ ] Zdefiniuj i opisz zmienność RTT; nie nazywaj jej bez wyjaśnienia jitterem UDP.
- [ ] Dodaj krótkie okna analizy (propozycja: 10 s i 1 min) oraz agregaty godzinowe i dobowe.
- [ ] Procent strat agreguj z liczników, nie przez nieważoną średnią procentów. Percentyli nie wyliczaj jako średniej percentyli podokresów.
- [ ] Dodaj konfigurowalne progi degradacji, okres stabilizacji przy zamykaniu incydentu i obsługę zbyt małej liczby próbek.
- [ ] Zachowuj przebieg i granice incydentu z informacją o rozdzielczości pomiaru.

Kryterium ukończenia: deterministyczne testy wykrywają krótką serię strat i skoki RTT, nie ukrywają ich w średniej dobowej oraz prawidłowo obsługują przerwy danych.

## Etap 4 — DNS, TCP i HTTPS

- [ ] Dodaj lekkie, okresowe sondy DNS i HTTPS do konfigurowalnych endpointów.
- [ ] Zapisuj kategorie błędów DNS, TCP, TLS i HTTP; rozdzielaj czasy etapów tam, gdzie metoda pomiaru to umożliwia.
- [ ] Rozróżniaj problem konkretnej usługi od ogólnej niedostępności sieci.
- [ ] Zapisuj użyty adres IP; analizuj IPv4 i IPv6 oddzielnie, jeśli są dostępne.

Kryterium ukończenia: kontrolowane błędy DNS, TLS i HTTP są rozpoznawane osobno; sukces TCP nie jest przedstawiany jako sukces HTTPS.

## Etap 5 — UDP i pomiary pod obciążeniem

Zależność: dostępny, kontrolowany lub uzgodniony serwer iperf3 poza domem. Nie wysyłaj testów UDP do przypadkowych publicznych usług. Sam brak konfiguracji serwera oznacza funkcję nieskonfigurowaną, nie awarię internetu.

- [ ] Rozbuduj iperf3 o UDP, upload i download oraz ustawienia przepływności, długości testu, rozmiaru datagramów i harmonogramu.
- [ ] Zapisuj statystyki odbiorcy: odebrane/utracone datagramy, procent strat i jitter; zachowuj wyniki cząstkowe i surowy JSON.
- [ ] Waliduj strukturę i błędy wyniku iperf3. Brak danych lub błąd narzędzia nie może dawać pozornych 0% strat.
- [ ] Ogranicz generowane obciążenie; zapobiegaj nakładaniu testów UDP i testów prędkości.
- [ ] Utrzymuj sondy opóźnienia podczas downloadu/uploadu i porównuj je z okresem bez testowego obciążenia.
- [ ] Oznaczaj testy obciążeniowe na osi czasu oraz w raportach.

Kryterium ukończenia: poprawny pomiar strat w obu kierunkach w kontrolowanym środowisku i rozróżnienie wyników zwykłego monitoringu od testów obciążeniowych.

## Etap 6 — diagnostyka incydentów

- [ ] Uruchamiaj MTR po wykryciu utrzymującej się degradacji do dotkniętych celów.
- [ ] Zachowuj wynik, surowy materiał, czas wykonania i błędy diagnostyki.
- [ ] Powiąż incydent z wynikami routera, innych celów i aktywnych testów obciążeniowych.
- [ ] Wprowadź limit częstotliwości i współbieżności diagnostyki.
- [ ] Prezentuj hipotezy lokalizacji problemu jako interpretacje, bez automatycznego przypisania winy operatorowi.

Kryterium ukończenia: incydent posiada powiązane pomiary i diagnostykę, a długotrwała degradacja nie wywołuje lawiny procesów MTR.

## Etap 7 — panel i raport dla dostawcy

Podstawowy zakres tego etapu realizuj już po etapie 3; kolejne metryki udostępniaj wraz z implementacją etapów 4–6.

- [ ] Dodaj osobny status dostępności i jakości oraz podpis „pomiar z NAS-a po kablu”.
- [ ] Dodaj wykresy strat i opóźnień per cel, wspólną oś incydentów, przerw i testów obciążeniowych.
- [ ] Dodaj szczegóły incydentu i ręczne oznaczenie objawu, np. „zacięcie TV”; oznaczenie użytkownika odróżniaj od pomiaru.
- [ ] Przygotuj raport HTML do druku/PDF za wybrany okres oraz CSV z danymi źródłowymi.
- [ ] Raport ma zawierać: czas i strefę, pokrycie danych, urządzenie/interfejs, metodę, konfigurację, cele, wersję aplikacji, liczniki, statystyki, incydenty, wykresy i ograniczenia.
- [ ] Rozdzielaj ICMP/TCP/UDP oraz kierunki UDP. Nie wyliczaj utraty pakietów ze starej historii TCP.
- [ ] Zapewnij zgodność liczb w panelu, API, CSV i raporcie, również dla granic zakresu czasu.

Kryterium ukończenia: raport jest czytelny bez ręcznego składania zrzutów ekranu, a jego liczby można odtworzyć z danych źródłowych. Brak danych jest widoczny.

## Etap 8 — retencja, weryfikacja i wdrożenie NAS

Podstawowe testy wykonuj na bieżąco; ten etap zamyka weryfikację całości.

- [ ] Dobierz konfigurowalną retencję: krótszą dla sond, dłuższą dla agregatów i incydentów; oszacuj przyrost bazy na dobę.
- [ ] Zdefiniuj dostępność surowych danych w raportach po upływie retencji; nie sugeruj pełnego eksportu, jeśli pozostały tylko agregaty.
- [ ] Zapewnij ograniczone buforowanie i obsługę błędów zapisu SQLite; określ możliwą utratę bufora przy nagłym zatrzymaniu.
- [ ] W odizolowanym środowisku zasymuluj straty, skoki opóźnienia, brak DNS, awarię jednego celu i restart monitora.
- [ ] Zweryfikuj migrację starej bazy, raportowanie i zużycie zasobów.
- [ ] Przygotuj instrukcję konfiguracji, aktualizacji, kopii bazy i wycofania wersji z uwzględnieniem migracji.
- [ ] W ramach osobno zleconego wdrożenia przeprowadź kilkudniowy pilotaż na NAS-ie, zapisując obciążenie CPU, RAM, dysku i stabilność zbierania danych.

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
