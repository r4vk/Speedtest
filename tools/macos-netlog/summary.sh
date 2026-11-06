#!/bin/bash
# Podsumowanie zebranych danych: straty per cel i godzina, zawieszenia, przerwy (uśpienie).
# Użycie: summary.sh [RRRR-MM-DD]   (domyślnie dziś)
set -u
LOG_DIR="${NETLOG_DIR:-$HOME/Library/Logs/r4vk-netlog}"
DAY="${1:-$(date '+%Y-%m-%d')}"
PING="$LOG_DIR/ping-$DAY.csv"
EVENTS="$LOG_DIR/events-$DAY.log"

[ -f "$PING" ] || { echo "Brak danych za $DAY ($PING)"; exit 1; }

echo "== $DAY — straty pingów (strata = brak odpowiedzi w 1 s)"
awk -F, 'NR>1 && $2=="up" {
    h=substr($1,12,2); n[h]++
    if ($3=="") r[h]++; if ($4=="") s[h]++; if ($5=="") i[h]++
    if ($3!="" && $3+0>100) sr[h]++
  }
  NR>1 && $2=="no_ip" { h=substr($1,12,2); noip[h]++ }
  END {
    printf "%-5s %7s %13s %13s %13s %11s %7s\n","godz","próbek","router","NAS","internet","router>100ms","bez IP"
    for (h=0; h<24; h++) { k=sprintf("%02d",h); if (!(k in n) && !(k in noip)) continue
      printf "%-5s %7d %6d (%4.1f%%) %6d (%4.1f%%) %6d (%4.1f%%) %11d %7d\n", k":00", n[k],
        r[k], n[k]?100*r[k]/n[k]:0, s[k], n[k]?100*s[k]/n[k]:0, i[k], n[k]?100*i[k]/n[k]:0, sr[k], noip[k] }
  }' "$PING"

echo
echo "== Jak interpretować:"
echo "   straty do routera i NAS-a     -> problem Wi-Fi / macOS (odcinek Mac–router)"
echo "   straty tylko do internetu     -> router lub operator (porównaj z panelem NAS)"

echo
echo "== Zawieszenia (router nie odpowiada ≥3 s)"
if [ -f "$EVENTS" ]; then grep -E 'ZAWIESZENIE|KONIEC|ZMIANA|START' "$EVENTS" || echo "brak"; else echo "brak"; fi

echo
echo "== Przerwy w pomiarach >10 s (zwykle uśpienie Maca)"
awk -F, 'NR>1 {
    t=substr($1,12,2)*3600 + substr($1,15,2)*60 + substr($1,18,2)
    if (p && t-p>10) printf "%s -> %s (%d s)\n", pt, substr($1,12,8), t-p
    p=t; pt=substr($1,12,8) }' "$PING" | tail -20
