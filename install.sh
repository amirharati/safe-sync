#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_DIR="$HOME/.safe-sync"
CONFIG="$CONFIG_DIR/config.json"
SOURCE="$ROOT_DIR/bin/safe-sync"
TRAY_LABEL="com.safe-sync.tray"
TRAY_PLIST="$HOME/Library/LaunchAgents/$TRAY_LABEL.plist"
APP_NAME="Safe Sync.app"
APP_INSTALL_DIR="${SAFE_SYNC_APP_DIR:-$HOME/Applications}"
APP_TARGET="$APP_INSTALL_DIR/$APP_NAME"

choose_bin_dir() {
  if [ -n "${SAFE_SYNC_BIN_DIR:-}" ]; then
    printf '%s\n' "$SAFE_SYNC_BIN_DIR"
  elif [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
    printf '%s\n' /usr/local/bin
  else
    printf '%s\n' "$HOME/.local/bin"
  fi
}

install_command() {
  BIN_DIR=$(choose_bin_dir)
  mkdir -p "$BIN_DIR"
  TARGET="$BIN_DIR/safe-sync"
  if ln -sfn "$SOURCE" "$TARGET" 2>/dev/null; then
    :
  else
    cp "$SOURCE" "$TARGET"
    chmod 755 "$TARGET"
  fi
  printf '%s\n' "$TARGET"
}


find_rclone() {
  if command -v rclone >/dev/null 2>&1; then
    command -v rclone
  elif [ -x /opt/homebrew/bin/rclone ]; then
    printf '%s\n' /opt/homebrew/bin/rclone
  elif [ -x /usr/local/bin/rclone ]; then
    printf '%s\n' /usr/local/bin/rclone
  else
    return 1
  fi
}

configure_rclone() {
  if RCLONE=$(find_rclone); then
    /usr/bin/python3 - "$CONFIG" "$RCLONE" <<'PYCONFIG'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1]).expanduser()
rclone = sys.argv[2]
config = json.loads(path.read_text())
if config.get("rclone_bin") != rclone:
    config["rclone_bin"] = rclone
    path.write_text(json.dumps(config, indent=2, sort_keys=True) + "\n")
PYCONFIG
    echo "rclone: $RCLONE"
  else
    echo "Error: rclone not found." >&2
    echo "Install it with: brew install rclone" >&2
    echo "Then run ./install.sh again." >&2
    exit 1
  fi
}

install_service_files() {
  PROGRAM="$1"
  TMP_DIR=$(mktemp -d "${TMPDIR:-/tmp}/safe-sync-install.XXXXXX")
  cleanup() {
    rm -rf "$TMP_DIR"
  }
  trap cleanup EXIT INT TERM
  "$SOURCE" render-install --output-dir "$TMP_DIR" --program "$PROGRAM" >/dev/null
  "$TMP_DIR/install-service.sh"
}

require_ui_tools() {
  command -v npm >/dev/null 2>&1 || {
    echo "npm is required to build the Safe Sync tray app." >&2
    exit 1
  }
  command -v cargo >/dev/null 2>&1 || {
    echo "cargo is required to build the Safe Sync tray app." >&2
    exit 1
  }
}

build_tray_app() {
  require_ui_tools
  (
    cd "$ROOT_DIR/ui"
    npm ci
    npm run tauri build
  )
}

install_tray_launch_agent() {
  mkdir -p "$HOME/Library/LaunchAgents"
  cat > "$TRAY_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$TRAY_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/open</string>
    <string>$APP_TARGET</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>EnvironmentVariables</key>
  <dict>
    <key>SAFE_SYNC_BIN</key>
    <string>$TARGET</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
EOF
  launchctl bootout "gui/$(id -u)" "$TRAY_PLIST" 2>/dev/null || true
  launchctl bootstrap "gui/$(id -u)" "$TRAY_PLIST" 2>/dev/null || true
  launchctl enable "gui/$(id -u)/$TRAY_LABEL" 2>/dev/null || true
}

install_tray_app() {
  case "$(uname -s)" in
    Darwin)
      ;;
    *)
      echo "Tray app install is TODO for $(uname -s); macOS is supported first." >&2
      return 0
      ;;
  esac

  build_tray_app
  APP_SOURCE="$ROOT_DIR/ui/src-tauri/target/release/bundle/macos/$APP_NAME"
  if [ ! -d "$APP_SOURCE" ]; then
    echo "Expected built app not found: $APP_SOURCE" >&2
    exit 1
  fi

  mkdir -p "$APP_INSTALL_DIR"
  rm -rf "$APP_TARGET"
  cp -R "$APP_SOURCE" "$APP_TARGET"
  install_tray_launch_agent
  /usr/bin/open "$APP_TARGET" 2>/dev/null || true
}

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
  "$SOURCE" init-config
fi

configure_rclone
TARGET=$(install_command)
install_service_files "$TARGET"

if [ "${SAFE_SYNC_INSTALL_UI:-1}" != "0" ]; then
  install_tray_app
fi

echo "Safe Sync installed."
echo "Command: $TARGET"
echo "Config: $CONFIG"
case ":$PATH:" in
  *":$(dirname "$TARGET"):"*)
    ;;
  *)
    echo "Warning: $(dirname "$TARGET") is not in PATH for this shell."
    echo "Add it to PATH or run: $TARGET"
    ;;
esac
echo "Backend service: ~/Library/LaunchAgents/com.safe-sync.daemon.plist"
if [ "${SAFE_SYNC_INSTALL_UI:-1}" != "0" ]; then
  echo "Tray app: $APP_TARGET"
  echo "Tray autostart: $TRAY_PLIST"
fi
echo "Start daemon: safe-sync start"
