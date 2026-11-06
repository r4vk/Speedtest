#!/bin/zsh
# Uruchamia zainstalowaną aplikację na danych tymczasowych, zbiera pomiary przez N s
# i zapisuje popover w trybie jasnym i ciemnym. Nie dotyka prawdziwych logów ani bazy.
# Użycie: tools/snap.sh [sekundy]   (domyślnie 25)
#         APP_SIMULATE_SLEEP=5,10 tools/snap.sh 30   — z symulacją uśpienia
set -euo pipefail
after=${1:-25}
work=${TMPDIR:-/tmp}/NetLog-snap
rm -rf "$work"; mkdir -p "$work"
NETLOG_DIR="$work/logs" APP_STORE="$work/netlog.sqlite" APP_SNAPSHOT="$work/shots" APP_SNAPSHOT_AFTER=$after \
  "${NETLOG_INSTALL_DIR:-/Applications}/NetLog.app/Contents/MacOS/NetLog"
echo "dane: $work"
