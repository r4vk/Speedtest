# NetLog (macos-agent)

Aplikacja paska menu macOS: pomiary sieci z Maca (Wi-Fi), następca `../tools/macos-netlog/`.
Opis, instalacja i format danych: [README.md](README.md). Brief: [docs/BRIEF.md](docs/BRIEF.md).
UI, komentarze i dziennik zdarzeń po polsku.

## Budowanie i sprawdzanie

Kod musi się budować także z samymi Command Line Tools, bez Xcode (`build.sh` używa Xcode, jeśli jest):
- nie używaj `@State` ani innych makr SwiftUI (wtyczka `SwiftUIMacros` jest tylko w Xcode). Stan trzymaj w `Monitor` (`@Observable`, z toolchainu);
- Swift Testing (`@Test`) i XCTest nie działają. Testy to program `Tests/NetLogCheck/main.swift`;
- `actool` nie istnieje, więc `build.sh` pomija ikonę.

Polecenia:
- `./build.sh` (`--open` restartuje aplikację). Bez ostrzeżeń.
- `swift run --scratch-path ~/Library/Developer/NetLog/build NetLogCheck` drukuje `PASS NetLogCheck`.
- `tools/snap.sh [s]` zapisuje PNG popovera (jasny/ciemny) z danych tymczasowych. Obejrzyj oba po każdej zmianie UI.
- Zrzuty z `snap.sh` rysują widok poza oknem i nie wychwycą złego rozmiaru okna (tak przeoczono zwinięty `Form`). Prawdziwe okno ustawień: `NETLOG_DEBUG=settings` z `NETLOG_DIR`/`APP_STORE` w katalogu tymczasowym wypisuje rozmiary okien na stderr.
- Nigdy nie testuj na `~/Library/Logs/r4vk-netlog` ani `~/Library/Application Support/NetLog`. Używaj `NETLOG_DIR` i `APP_STORE` w katalogu tymczasowym (robi to `snap.sh`).
- Nie symuluj awarii na prawdziwym łączu. Zamiast tego: `NETLOG_ROUTER_IP=192.0.2.1` (milczący router), `APP_SIMULATE_SLEEP=a,b`.

## Zasady

- Format CSV i słowa kluczowe dziennika (`START`, `ZAWIESZENIE`, `KONIEC`, `ZMIANA`) muszą zostać zgodne z `summary.sh`.
- Uśpienie, przerwa i brak IP to nigdy strata pakietów. Zapisuj je jako zdarzenia.
- `NetLogKit` nie zna SwiftUI. Blokujące wywołania (ICMP, `Process`) działają poza wątkiem głównym (`@concurrent` lub własna kolejka).
- Bez `.terminateLater` przy wyjściu: zadania Swift nie ruszają w modalnej pętli AppKit (zakleszczenie).
- Etap 9 (wysyłka na NAS) dopiero po części NAS. Schemat `probe_results` trzymaj zgodny z `ProbeResult` serwera.
