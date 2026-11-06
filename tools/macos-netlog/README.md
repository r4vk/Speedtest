# macos-netlog — lokalny logger sieci na Macu (Wi-Fi)

Tymczasowe narzędzie do etapu 9 (przed pełnym agentem z wysyłką do NAS). Działa w tle jako LaunchAgent
i odpowiada na pytanie: **czy Mac gubi pakiety na odcinku Wi-Fi, czy problem jest dalej (router/operator)?**

## Co zbiera

| Plik (`~/Library/Logs/r4vk-netlog/`) | Zawartość |
|---|---|
| `ping-RRRR-MM-DD.csv` | co 1 s: ping do routera (wykrywany automatycznie), NAS-a (192.168.10.2) i 1.1.1.1; pusta wartość = brak odpowiedzi w 1 s; `link=no_ip` = brak adresu IP (to nie jest strata) |
| `wifi-RRRR-MM-DD.csv` | co 60 s: sygnał, szum, kanał, prędkość, MCS, stan AWDL |
| `events-RRRR-MM-DD.log` | zawieszenia (router nie odpowiada ≥3 s), ich koniec i czas trwania, zmiany routera; przy zawieszeniu zrzut: Wi-Fi, ARP routera, trasa, HTTP do NAS-a, DNS |

Luka w znacznikach czasu = Mac spał. Pliki starsze niż 30 dni są usuwane automatycznie.

## Obsługa

```bash
./install.sh                                                   # instalacja i start (także po każdej zmianie skryptu)
"$HOME/Library/Application Support/r4vk-netlog/summary.sh"     # podsumowanie za dziś
"$HOME/Library/Application Support/r4vk-netlog/summary.sh" 2026-11-07
./uninstall.sh                                                 # zatrzymanie (logi zostają)
```

Cele można zmienić zmiennymi przy instalacji: `NETLOG_NAS_IP`, `NETLOG_NAS_URL`, `NETLOG_INTERNET_IP`.

## Jak czytać wyniki

- Straty jednocześnie do routera, NAS-a i internetu → odcinek Mac–router (Wi-Fi lub macOS).
- Straty tylko do internetu → router albo operator; porównaj czas z panelem NAS (`http://192.168.10.2:18000`).
- W zrzucie `ARP routera … (incomplete)` → Mac zgubił łączność na poziomie Wi-Fi/L2, mimo że pokazuje połączenie.
- Zawieszenia zbiegające się z `awdl=active` i skokami opóźnień → podejrzany AWDL (AirDrop/Handoff).
