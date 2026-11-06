#!/bin/bash
# Zatrzymuje i usuwa LaunchAgent. Zebrane logi zostają w ~/Library/Logs/r4vk-netlog.
set -u
LABEL="org.r4vk.netlog"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$LABEL.plist"
rm -rf "$HOME/Library/Application Support/r4vk-netlog"
echo "Odinstalowano. Logi zostały w $HOME/Library/Logs/r4vk-netlog"
