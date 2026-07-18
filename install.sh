#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_DIR="$HOME/.safe-sync"
CONFIG="$CONFIG_DIR/config.json"
RUNTIME_DIR="${SAFE_SYNC_RUNTIME_DIR:-$HOME/.local/share/safe-sync}"
RUNTIME_CURRENT="$RUNTIME_DIR/current"
SOURCE=""
PYTHON_BIN="${PYTHON_BIN:-python3}"
RCLONE_VERSION="v1.74.4"
TRAY_LABEL="com.safe-sync.tray"
TRAY_PLIST="$HOME/Library/LaunchAgents/$TRAY_LABEL.plist"
APP_NAME="Safe Sync.app"
APP_INSTALL_DIR="${SAFE_SYNC_APP_DIR:-$HOME/Applications}"
APP_TARGET="$APP_INSTALL_DIR/$APP_NAME"
LINUX_UI_DIR="${SAFE_SYNC_LINUX_UI_DIR:-$HOME/.local/share/safe-sync/ui}"
LINUX_APP_TARGET="$LINUX_UI_DIR/safe-sync-ui"
LINUX_DESKTOP_DIR="$HOME/.local/share/applications"
LINUX_AUTOSTART_DIR="$HOME/.config/autostart"
LINUX_ICON_DIR="$HOME/.local/share/icons/hicolor/128x128/apps"
LINUX_DESKTOP_FILE="$LINUX_DESKTOP_DIR/safe-sync.desktop"
LINUX_AUTOSTART_FILE="$LINUX_AUTOSTART_DIR/safe-sync.desktop"

INSTALL_UI=1
UPDATE=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [--headless] [--update]

  --headless  Install the CLI and daemon only; do not build the desktop app.
  --update     Upgrade the installed Safe Sync runtime in place.
EOF
}

while [ "$#" -gt 0 ]; do
  case "$1" in
    --headless)
      INSTALL_UI=0
      ;;
    --update)
      UPDATE=1
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "Unknown install option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
  shift
done

if [ "${SAFE_SYNC_INSTALL_UI:-1}" = "0" ]; then
  INSTALL_UI=0
fi

require_python() {
  command -v "$PYTHON_BIN" >/dev/null 2>&1 || {
    echo "Python 3 is required. Install python3, then run ./install.sh again." >&2
    exit 1
  }
}

require_runtime_tools() {
  command -v curl >/dev/null 2>&1 || {
    echo "curl is required to download Safe Sync's managed rclone runtime." >&2
    exit 1
  }
  command -v unzip >/dev/null 2>&1 || {
    echo "unzip is required to unpack Safe Sync's managed rclone runtime." >&2
    exit 1
  }
  if ! command -v shasum >/dev/null 2>&1 && ! command -v sha256sum >/dev/null 2>&1; then
    echo "A SHA-256 tool (shasum or sha256sum) is required to verify rclone." >&2
    exit 1
  fi
}

stage_runtime() {
  mkdir -p "$RUNTIME_DIR"
  STAGE_DIR=$(mktemp -d "$RUNTIME_DIR/.stage.XXXXXX")
  cp -R "$ROOT_DIR/bin" "$ROOT_DIR/config" "$ROOT_DIR/src" "$STAGE_DIR/"
  SOURCE="$STAGE_DIR/bin/safe-sync"
}

discard_staged_runtime() {
  if [ -n "${STAGE_DIR:-}" ] && [ -d "$STAGE_DIR" ]; then
    rm -rf "$STAGE_DIR"
  fi
}

activate_staged_runtime() {
  # Download/verification happens before this point, so a failed dependency
  # install cannot replace a working runtime.
  rm -rf "$RUNTIME_DIR/previous"
  if [ -e "$RUNTIME_CURRENT" ]; then
    mv "$RUNTIME_CURRENT" "$RUNTIME_DIR/previous"
  fi
  mv "$STAGE_DIR" "$RUNTIME_CURRENT"
  STAGE_DIR=""
  SOURCE="$RUNTIME_CURRENT/bin/safe-sync"
  rm -rf "$RUNTIME_DIR/previous"
}

choose_bin_dir() {
  if [ -n "${SAFE_SYNC_BIN_DIR:-}" ]; then
    printf '%s\n' "$SAFE_SYNC_BIN_DIR"
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

ensure_command_on_path() {
  COMMAND_DIR="$1"
  case "$(uname -s):${SHELL:-}" in
    Darwin:*) PROFILE="$HOME/.zshrc" ;;
    Linux:*) PROFILE="$HOME/.bashrc" ;;
    *) PROFILE="$HOME/.profile" ;;
  esac
  PATH_EXPORT="export PATH=\"$COMMAND_DIR:\$PATH\""
  if ! grep -Fqx "$PATH_EXPORT" "$PROFILE" 2>/dev/null; then
    {
      printf '\n# Added by Safe Sync\n%s\n' "$PATH_EXPORT"
    } >> "$PROFILE"
  fi
  PATH_PROFILE="$PROFILE"
  case ":$PATH:" in
    *":$COMMAND_DIR:"*) return 0 ;;
    *) return 1 ;;
  esac
}


managed_rclone_asset() {
  case "$(uname -s):$(uname -m)" in
    Darwin:arm64)
      RCLONE_ASSET="rclone-${RCLONE_VERSION}-osx-arm64.zip"
      RCLONE_SHA256="c2100e2d4a4b3be04c55cd45380cafe7647e1ad772bb055f52f00876ed701167"
      ;;
    Darwin:x86_64)
      RCLONE_ASSET="rclone-${RCLONE_VERSION}-osx-amd64.zip"
      RCLONE_SHA256="4188aa84043d7a6240912923f47639a9d2da21f3b40a521c065c8d92e66563f6"
      ;;
    Linux:x86_64)
      RCLONE_ASSET="rclone-${RCLONE_VERSION}-linux-amd64.zip"
      RCLONE_SHA256="fe435e0c36228e7c2f116a8701f01127bb1f694005fc11d1f27186c8bca4115d"
      ;;
    Linux:aarch64|Linux:arm64)
      RCLONE_ASSET="rclone-${RCLONE_VERSION}-linux-arm64.zip"
      RCLONE_SHA256="97685285c9ad6a0cf17d5844115d2a67245af6444db672187074bd9c358de419"
      ;;
    *)
      echo "No managed rclone build is defined for $(uname -s) $(uname -m)." >&2
      exit 1
      ;;
  esac
}

sha256_file() {
  if command -v shasum >/dev/null 2>&1; then
    shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then
    sha256sum "$1" | awk '{print $1}'
  else
    echo "A SHA-256 tool (shasum or sha256sum) is required." >&2
    exit 1
  fi
}

install_managed_rclone() {
  managed_rclone_asset
  RCLONE_TARGET="$RUNTIME_DIR/rclone/$RCLONE_VERSION/rclone"
  if [ -x "$RCLONE_TARGET" ]; then
    printf '%s\n' "$RCLONE_TARGET"
    return
  fi
  command -v curl >/dev/null 2>&1 || {
    echo "curl is required to install Safe Sync's managed rclone runtime." >&2
    exit 1
  }
  command -v unzip >/dev/null 2>&1 || {
    echo "unzip is required to install Safe Sync's managed rclone runtime." >&2
    exit 1
  }
  DOWNLOAD_DIR=$(mktemp -d "${TMPDIR:-/tmp}/safe-sync-rclone.XXXXXX")
  cleanup_rclone_download() { rm -rf "$DOWNLOAD_DIR"; }
  trap cleanup_rclone_download EXIT INT TERM
  ARCHIVE="$DOWNLOAD_DIR/$RCLONE_ASSET"
  curl -fsSL "https://downloads.rclone.org/$RCLONE_VERSION/$RCLONE_ASSET" -o "$ARCHIVE"
  ACTUAL_SHA256=$(sha256_file "$ARCHIVE")
  if [ "$ACTUAL_SHA256" != "$RCLONE_SHA256" ]; then
    echo "Managed rclone checksum verification failed." >&2
    exit 1
  fi
  unzip -q "$ARCHIVE" -d "$DOWNLOAD_DIR/unpacked"
  RCLONE_SOURCE=$(find "$DOWNLOAD_DIR/unpacked" -type f -name rclone -perm -u+x | head -n 1)
  if [ -z "$RCLONE_SOURCE" ]; then
    echo "Managed rclone archive did not contain an executable." >&2
    exit 1
  fi
  mkdir -p "$(dirname "$RCLONE_TARGET")"
  cp "$RCLONE_SOURCE" "$RCLONE_TARGET"
  chmod 755 "$RCLONE_TARGET"
  "$RCLONE_TARGET" version >/dev/null
  trap - EXIT INT TERM
  cleanup_rclone_download
  printf '%s\n' "$RCLONE_TARGET"
}

configure_rclone() {
  RCLONE="$1"
  "$PYTHON_BIN" - "$CONFIG" "$RCLONE" <<'PYCONFIG'
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
  echo "managed rclone: $RCLONE"
}

has_enabled_folders() {
  "$PYTHON_BIN" - "$CONFIG" <<'PYFOLDERS'
import json
import sys

config = json.loads(open(sys.argv[1]).read())
active = config.get("active_profile_id")
profiles = config.get("profiles", [])
profile = next((item for item in profiles if item.get("id") == active), {})
folders = profile.get("folders", config.get("folders", []))
raise SystemExit(0 if any(folder.get("enabled", True) for folder in folders) else 1)
PYFOLDERS
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
    case "$(uname -s)" in
      Linux)
        # The source installer owns a user-scoped executable and XDG entries;
        # it does not need a system .deb/.rpm package or root privileges.
        npm run tauri -- build --no-bundle
        ;;
      *)
        npm run tauri build
        ;;
    esac
  )
}

stop_tray_app() {
  case "$(uname -s)" in
    Darwin|Linux)
      # The LaunchAgent owns /usr/bin/open, not the app process itself. Stop the
      # previous bundle before replacing it so an update cannot leave it alive.
      pkill -x safe-sync-ui 2>/dev/null || true
      ;;
  esac
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
      build_tray_app
      APP_SOURCE="$ROOT_DIR/ui/src-tauri/target/release/bundle/macos/$APP_NAME"
      if [ ! -d "$APP_SOURCE" ]; then
        echo "Expected built app not found: $APP_SOURCE" >&2
        exit 1
      fi

      stop_tray_app
      mkdir -p "$APP_INSTALL_DIR"
      rm -rf "$APP_TARGET"
      cp -R "$APP_SOURCE" "$APP_TARGET"
      install_tray_launch_agent
      /usr/bin/open "$APP_TARGET" 2>/dev/null || true
      TRAY_MESSAGE="Tray app: $APP_TARGET"
      TRAY_AUTOSTART_MESSAGE="Tray autostart: $TRAY_PLIST"
      ;;
    Linux)
      build_tray_app
      APP_SOURCE="$ROOT_DIR/ui/src-tauri/target/release/safe-sync-ui"
      if [ ! -x "$APP_SOURCE" ]; then
        echo "Expected built Linux tray executable not found: $APP_SOURCE" >&2
        exit 1
      fi

      stop_tray_app
      mkdir -p "$LINUX_UI_DIR" "$LINUX_DESKTOP_DIR" "$LINUX_AUTOSTART_DIR" "$LINUX_ICON_DIR"
      cp "$APP_SOURCE" "$LINUX_APP_TARGET"
      chmod 755 "$LINUX_APP_TARGET"
      cp "$ROOT_DIR/ui/src-tauri/icons/128x128.png" "$LINUX_ICON_DIR/safe-sync.png"
      cat > "$LINUX_DESKTOP_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Safe Sync
Comment=Safe Sync tray and control panel
Exec=/usr/bin/env SAFE_SYNC_BIN=$TARGET $LINUX_APP_TARGET
TryExec=$LINUX_APP_TARGET
Icon=safe-sync
Terminal=false
Categories=Utility;
EOF
      cat > "$LINUX_AUTOSTART_FILE" <<EOF
[Desktop Entry]
Type=Application
Name=Safe Sync
Exec=/usr/bin/env SAFE_SYNC_BIN=$TARGET $LINUX_APP_TARGET
TryExec=$LINUX_APP_TARGET
Icon=safe-sync
Terminal=false
X-GNOME-Autostart-enabled=true
EOF
      if command -v update-desktop-database >/dev/null 2>&1; then
        update-desktop-database "$LINUX_DESKTOP_DIR" 2>/dev/null || true
      fi
      SAFE_SYNC_BIN="$TARGET" "$LINUX_APP_TARGET" >/dev/null 2>&1 &
      TRAY_MESSAGE="Tray app: $LINUX_APP_TARGET"
      TRAY_AUTOSTART_MESSAGE="Tray launcher: $LINUX_DESKTOP_FILE; autostart: $LINUX_AUTOSTART_FILE"
      ;;
    *)
      echo "Tray app install is not supported for $(uname -s). Use --headless." >&2
      return 0
      ;;
  esac
}

require_python
require_runtime_tools
if [ "$INSTALL_UI" = "1" ]; then
  require_ui_tools
fi
stage_runtime
trap discard_staged_runtime EXIT INT TERM
mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
  "$SOURCE" init-config
fi

RCLONE=$(install_managed_rclone)
configure_rclone "$RCLONE"
activate_staged_runtime
TARGET=$(install_command)
if ensure_command_on_path "$(dirname "$TARGET")"; then
  PATH_MESSAGE="Command directory is already in PATH."
else
  PATH_MESSAGE="Command directory was added to $PATH_PROFILE for new shell sessions. Open a new terminal to use safe-sync by name."
fi
install_service_files "$TARGET"
if has_enabled_folders; then
  "$TARGET" restart >/dev/null
  BACKEND_MESSAGE="Backend service installed and started."
else
  BACKEND_MESSAGE="Backend service installed. It will start after safe-sync setup adds a folder."
fi

if [ "$INSTALL_UI" = "1" ]; then
  install_tray_app
fi
trap - EXIT INT TERM

if [ "$UPDATE" = "1" ]; then
  echo "Safe Sync updated."
else
  echo "Safe Sync installed."
fi
echo "Command: $TARGET"
echo "Config: $CONFIG"
echo "Runtime: $RUNTIME_CURRENT"
echo "$PATH_MESSAGE"
echo "$BACKEND_MESSAGE"
if [ "$INSTALL_UI" = "1" ]; then
  echo "$TRAY_MESSAGE"
  echo "$TRAY_AUTOSTART_MESSAGE"
fi
echo "Next step: safe-sync setup"
