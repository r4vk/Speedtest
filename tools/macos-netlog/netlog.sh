#!/bin/bash
# Lokalny logger jakości sieci dla macOS (Wi-Fi).
# Co sekundę pinguje router, NAS i internet; co minutę zapisuje stan Wi-Fi;
# przy zaniku łączności robi zrzut diagnostyczny (Wi-Fi, ARP, trasy, HTTP do NAS).
# Działa jako LaunchAgent (patrz install.sh). Zgodny z /bin/bash 3.2.

set -u

NAS_IP="${NETLOG_NAS_IP:-192.168.10.2}"
NAS_URL="${NETLOG_NAS_URL:-http://192.168.10.2:18000/}"
INTERNET_IP="${NETLOG_INTERNET_IP:-1.1.1.1}"
IFACE="${NETLOG_IFACE:-en0}"
LOG_DIR="${NETLOG_DIR:-$HOME/Library/Logs/r4vk-netlog}"
KEEP_DAYS="${NETLOG_KEEP_DAYS:-30}"
PING_TIMEOUT_S=1         # brak odpowiedzi w tym czasie = strata
STUCK_AFTER=3             # tyle kolejnych strat do routera = zawieszenie, robimy zrzut
SNAPSHOT_EVERY=30         # w trakcie zawieszenia zrzut nie częściej niż co tyle sekund

mkdir -p "$LOG_DIR"
TMP="$(mktemp -d -t netlog)"
trap 'rm -rf "$TMP"; kill 0 2>/dev/null' EXIT
trap 'exit 0' TERM INT

now_iso() { date '+%Y-%m-%dT%H:%M:%S%z'; }
today() { date '+%Y-%m-%d'; }

gateway() { route -n get default 2>/dev/null | awk '/gateway:/{print $2; exit}'; }

# Wynik: czas w ms albo pusty napis przy stracie.
ping_once() {
  ping -n -c 1 -t "$PING_TIMEOUT_S" "$1" 2>/dev/null | awk -F'time=' '/time=/{split($2,a," "); print a[1]; exit}'
}

# Jedna linia CSV ze stanem Wi-Fi: rssi,noise,channel,band,width,tx_rate,mcs,phy,awdl
wifi_state() {
  local sp awdl
  sp="$(system_profiler SPAirPortDataType 2>/dev/null | awk '/Current Network Information:/{f=1;next} f&&/Other Local/{exit} f')"
  awdl="$(ifconfig awdl0 2>/dev/null | awk '/status:/{print $2; exit}')"
  printf '%s\n' "$sp" | awk -v awdl="${awdl:-none}" '
    /Signal \/ Noise:/ { split($0,a,":"); gsub(/ dBm/,"",a[2]); split(a[2],b,"/"); rssi=b[1]+0; noise=b[2]+0 }
    /Channel:/        { ch=$2; band=$3; gsub(/[(,]/,"",band); width=$4; gsub(/[)]/,"",width) }
    /Transmit Rate:/  { tx=$3 }
    /MCS Index:/      { mcs=$3 }
    /PHY Mode:/       { phy=$3 }
    END { printf "%s,%s,%s,%s,%s,%s,%s,%s,%s\n", rssi, noise, ch, band, width, tx, mcs, phy, awdl }'
}

wifi_logger() {
  while true; do
    local f="$LOG_DIR/wifi-$(today).csv"
    [ -f "$f" ] || echo "ts,rssi_dbm,noise_dbm,channel,band,width,tx_rate_mbps,mcs,phy,awdl" > "$f"
    echo "$(now_iso),$(wifi_state)" >> "$f"
    sleep 60
  done
}

snapshot() {
  local gw="$1" reason="$2" f="$LOG_DIR/events-$(today).log"
  {
    echo "=== $(now_iso) ZRZUT: $reason"
    echo "--- Wi-Fi"
    system_profiler SPAirPortDataType 2>/dev/null | awk '/Current Network Information:/{f=1;next} f&&/Other Local/{exit} f'
    echo "--- ifconfig $IFACE"
    ifconfig "$IFACE" 2>&1 | grep -E 'status|inet |ether|media'
    echo "--- awdl0: $(ifconfig awdl0 2>/dev/null | awk '/status:/{print $2}')"
    echo "--- ARP routera ($gw)"
    arp -n "$gw" 2>&1
    echo "--- trasa domyślna"
    route -n get default 2>&1 | grep -E 'gateway|interface'
    echo "--- HTTP do NAS ($NAS_URL)"
    curl -s -o /dev/null -w 'http=%{http_code} czas=%{time_total}s\n' --max-time 3 "$NAS_URL" 2>&1 || echo "curl nieudany"
    echo "--- DNS"
    scutil --dns 2>/dev/null | awk '/nameserver\[/{print}' | sort -u | head -4
    echo
  } >> "$f" 2>&1
}

event() { echo "$(now_iso) $*" >> "$LOG_DIR/events-$(today).log"; }

cleanup_old() { find "$LOG_DIR" -type f -name '*-20*' -mtime +"$KEEP_DAYS" -delete 2>/dev/null; }

wifi_logger &

GW="$(gateway)"
GW_CHECK=0
LOSS_RUN=0            # kolejne straty do routera
INCIDENT_START=""     # epoch początku zawieszenia
INCIDENT_LOST=0
LAST_SNAPSHOT=0
LAST_DAY=""
event "START logger, router=${GW:-?} nas=$NAS_IP internet=$INTERNET_IP iface=$IFACE"

while true; do
  tick=$(date +%s)
  day="$(today)"
  if [ "$day" != "$LAST_DAY" ]; then cleanup_old; LAST_DAY="$day"; fi

  if [ $((tick - GW_CHECK)) -ge 60 ] || [ -z "$GW" ]; then
    new_gw="$(gateway)"
    if [ -n "$new_gw" ] && [ "$new_gw" != "$GW" ]; then event "ZMIANA routera: ${GW:-brak} -> $new_gw"; fi
    [ -n "$new_gw" ] && GW="$new_gw"
    GW_CHECK=$tick
  fi

  ip="$(ipconfig getifaddr "$IFACE" 2>/dev/null)"
  f="$LOG_DIR/ping-$day.csv"
  [ -f "$f" ] || echo "ts,link,router_ms,nas_ms,internet_ms" > "$f"

  if [ -z "$ip" ]; then
    # Brak adresu IP (Wi-Fi wyłączone, roaming, budzenie) — to nie jest strata pakietów.
    echo "$(now_iso),no_ip,,," >> "$f"
  else
    sleep 1 & p0=$!
    ping_once "$GW" > "$TMP/r" & p1=$!
    ping_once "$NAS_IP" > "$TMP/n" & p2=$!
    ping_once "$INTERNET_IP" > "$TMP/i" & p3=$!
    wait $p1 $p2 $p3
    r="$(cat "$TMP/r")"; n="$(cat "$TMP/n")"; i="$(cat "$TMP/i")"
    echo "$(now_iso),up,$r,$n,$i" >> "$f"

    if [ -z "$r" ]; then
      LOSS_RUN=$((LOSS_RUN + 1))
      if [ "$LOSS_RUN" -ge "$STUCK_AFTER" ]; then
        if [ -z "$INCIDENT_START" ]; then
          INCIDENT_START=$((tick - STUCK_AFTER + 1)); INCIDENT_LOST=$((STUCK_AFTER - 1))
          event "ZAWIESZENIE: router nie odpowiada od $STUCK_AFTER s (nas=${n:-strata} internet=${i:-strata})"
        fi
        INCIDENT_LOST=$((INCIDENT_LOST + 1))
        if [ $((tick - LAST_SNAPSHOT)) -ge "$SNAPSHOT_EVERY" ]; then
          LAST_SNAPSHOT=$tick
          snapshot "$GW" "router nie odpowiada od $LOSS_RUN s" &
        fi
      fi
    else
      if [ -n "$INCIDENT_START" ]; then
        event "KONIEC zawieszenia: trwało $((tick - INCIDENT_START)) s, utracone pingi do routera: $INCIDENT_LOST"
        INCIDENT_START=""; INCIDENT_LOST=0
      fi
      LOSS_RUN=0
    fi
  fi

  # Jeden obieg ≈ 1 s. Luka w znacznikach czasu = Mac spał (to nie awaria sieci).
  if [ -n "${p0:-}" ]; then wait "$p0" 2>/dev/null; p0=""; else sleep 1; fi
done
