#!/bin/bash
# Instaluje logger jako LaunchAgent użytkownika (startuje przy logowaniu, restartuje się po awarii).
set -euo pipefail

LABEL="org.r4vk.netlog"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/Library/Application Support/r4vk-netlog"
LOG_DIR="$HOME/Library/Logs/r4vk-netlog"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

mkdir -p "$APP_DIR" "$LOG_DIR" "$HOME/Library/LaunchAgents"
cp "$SRC_DIR/netlog.sh" "$SRC_DIR/summary.sh" "$APP_DIR/"
chmod +x "$APP_DIR/netlog.sh" "$APP_DIR/summary.sh"

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key>
  <array><string>/bin/bash</string><string>$APP_DIR/netlog.sh</string></array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>NETLOG_NAS_IP</key><string>${NETLOG_NAS_IP:-192.168.10.2}</string>
    <key>NETLOG_NAS_URL</key><string>${NETLOG_NAS_URL:-http://192.168.10.2:18000/}</string>
    <key>NETLOG_INTERNET_IP</key><string>${NETLOG_INTERNET_IP:-1.1.1.1}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ProcessType</key><string>Background</string>
  <key>StandardOutPath</key><string>$LOG_DIR/netlog.stdout.log</string>
  <key>StandardErrorPath</key><string>$LOG_DIR/netlog.stderr.log</string>
</dict>
</plist>
EOF

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
echo "Zainstalowano. Logi: $LOG_DIR"
echo "Podsumowanie: \"$APP_DIR/summary.sh\""
