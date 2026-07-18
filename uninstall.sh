#!/bin/sh
set -eu

PURGE=0
RUNTIME_DIR="${SAFE_SYNC_RUNTIME_DIR:-$HOME/.local/share/safe-sync}"
BIN_DIR="${SAFE_SYNC_BIN_DIR:-$HOME/.local/bin}"
COMMAND="$BIN_DIR/safe-sync"
CONFIG_DIR="$HOME/.safe-sync"
STATE_DIR="$HOME/.local/state/safe-sync"
LOG_DIR="$HOME/.local/log/safe-sync"
DAEMON_LABEL="com.safe-sync.daemon"
TRAY_LABEL="com.safe-sync.tray"
DAEMON_PLIST="$HOME/Library/LaunchAgents/$DAEMON_LABEL.plist"
TRAY_PLIST="$HOME/Library/LaunchAgents/$TRAY_LABEL.plist"
APP_TARGET="${SAFE_SYNC_APP_DIR:-$HOME/Applications}/Safe Sync.app"
LINUX_DESKTOP_FILE="$HOME/.local/share/applications/safe-sync.desktop"
LINUX_AUTOSTART_FILE="$HOME/.config/autostart/safe-sync.desktop"
LINUX_ICON_FILE="$HOME/.local/share/icons/hicolor/128x128/apps/safe-sync.png"

usage() {
  cat <<'EOF'
Usage: ./uninstall.sh [--purge]

  --purge  Also remove Safe Sync configuration, state, logs, and managed
           Dropbox authorization after an explicit confirmation.

Normal uninstall preserves ~/.safe-sync and never deletes Dropbox backups.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --purge) PURGE=1 ;;
    --help|-h) usage; exit 0 ;;
    *) echo "Unknown uninstall option: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

if [ "$PURGE" = "1" ] && [ "${SAFE_SYNC_PURGE_CONFIRM:-}" != "REMOVE" ]; then
  printf 'Type REMOVE to delete Safe Sync local configuration and state: '
  read -r confirmation
  if [ "$confirmation" != "REMOVE" ]; then
    echo "Purge cancelled."
    exit 1
  fi
fi

case "$(uname -s)" in
  Darwin)
    launchctl bootout "gui/$(id -u)" "$TRAY_PLIST" 2>/dev/null || true
    launchctl bootout "gui/$(id -u)" "$DAEMON_PLIST" 2>/dev/null || true
    rm -f "$TRAY_PLIST" "$DAEMON_PLIST"
    rm -rf "$APP_TARGET"
    ;;
  Linux)
    systemctl --user disable --now safe-sync-daemon.service 2>/dev/null || true
    rm -f "$HOME/.config/systemd/user/safe-sync-daemon.service"
    rm -f "$LINUX_DESKTOP_FILE" "$LINUX_AUTOSTART_FILE" "$LINUX_ICON_FILE"
    systemctl --user daemon-reload 2>/dev/null || true
    ;;
  *)
    echo "Safe Sync service removal is not implemented for $(uname -s)." >&2
    ;;
esac

if [ -L "$COMMAND" ]; then
  rm -f "$COMMAND"
elif [ -f "$COMMAND" ] && grep -q "Executable wrapper for Safe Sync" "$COMMAND" 2>/dev/null; then
  rm -f "$COMMAND"
fi

rm -rf "$RUNTIME_DIR" "$STATE_DIR" "$LOG_DIR"

if [ "$PURGE" = "1" ]; then
  rm -rf "$CONFIG_DIR"
  echo "Safe Sync removed, including local configuration and authorization."
else
  echo "Safe Sync removed. Configuration and authorization were preserved at $CONFIG_DIR."
fi
echo "Dropbox backups and remote trash were not changed."
