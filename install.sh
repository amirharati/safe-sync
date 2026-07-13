#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
CONFIG_DIR="$HOME/.safe-sync"
CONFIG="$CONFIG_DIR/config.json"
SOURCE="$ROOT_DIR/bin/safe-sync"

choose_bin_dir() {
  if [ -n "${SAFE_SYNC_BIN_DIR:-}" ]; then
    printf '%s
' "$SAFE_SYNC_BIN_DIR"
  elif [ -d /usr/local/bin ] && [ -w /usr/local/bin ]; then
    printf '%s
' /usr/local/bin
  else
    printf '%s
' "$HOME/.local/bin"
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
  printf '%s
' "$TARGET"
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

mkdir -p "$CONFIG_DIR"
if [ ! -f "$CONFIG" ]; then
  "$SOURCE" init-config
fi

TARGET=$(install_command)
install_service_files "$TARGET"

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
echo "Start daemon: safe-sync start"
