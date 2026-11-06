# NetLog

Aplikacja w pasku menu macOS, która co sekundę sprawdza, czy Mac na Wi-Fi faktycznie przepuszcza
ruch (router, NAS, internet), i zapisuje dowody, gdy przestaje. Dla jednej osoby: właściciela Maca
i sieci domowej. *(Wszystkie wybory poniżej: decided by the agent, na podstawie `tools/macos-netlog/`
i `NETWORK_QUALITY_PLAN.md` etap 9).*

## Replaces
Logger powłoki `tools/macos-netlog/netlog.sh` (LaunchAgent `org.r4vk.netlog`).

## Main jobs
1. Zbierać w tle: ICMP co 1 s do routera (wykrywanego automatycznie), NAS-a (192.168.10.2) i 1.1.1.1;
   stan Wi-Fi co 60 s z CoreWLAN; zawieszenia (router milczy ≥ 3 s) ze zrzutem diagnostycznym.
2. Pokazywać na żywo w pasku menu: ok / opóźnienia / zawieszenie / brak IP, a w popoverze wykres
   ostatnich 5 minut, bieżące Wi-Fi i ostatnie zdarzenia.
3. Nie liczyć uśpienia Maca jako awarii: wstrzymać pomiary na czas snu, po wybudzeniu dać chwilę na
   ponowne połączenie i zapisać przerwę jawnie.

## Data
- Próba (ProbeResult): czas UTC, cel (router/nas/internet), wynik ok/timeout/error, RTT, próg, łącze.
- Próbka Wi-Fi: RSSI, szum, kanał, pasmo, szerokość, tx rate, MCS, PHY, AWDL.
- Zdarzenie: START, ZAWIESZENIE, KONIEC, ZMIANA, USYPIANIE, WYBUDZENIE, PRZERWA + zrzut.

## Shape
Menu bar extra (bez okna i bez ikony w Docku), macOS 26 lub nowszy. Dane lokalne: pliki CSV
zgodne z loggerem (`~/Library/Logs/r4vk-netlog/`) i SQLite z kolejką do wysłania w etapie 9.
Działa offline. Brak kont.

## System hooks
Pasek menu, start przy logowaniu (SMAppService), powiadomienia NSWorkspace o uśpieniu/wybudzeniu,
SystemConfiguration (router, IP), CoreWLAN.

## Reference
Brak. Wygląd: jak systemowe dodatki paska menu.

## Look
Natywny, kolor akcentu systemowy. Kolor ikony tylko jako sygnał stanu: pomarańczowy = opóźnienia,
czerwony = zawieszenie.

## Not in version one
- Historia dłuższa niż 5 minut w UI (od tego jest `summary.sh` i panel NAS).
