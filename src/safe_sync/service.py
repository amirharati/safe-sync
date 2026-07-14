'''OS service integration for Safe Sync.'''

from __future__ import annotations

import os
import platform
import re
import subprocess
from pathlib import Path


DEFAULT_LOG_DIR = Path.home() / '.local' / 'log' / 'safe-sync'
LAUNCHD_LABEL = 'com.safe-sync.daemon'


def os_name() -> str:
    return platform.system()


def launchd_plist_path() -> Path:
    return Path.home() / 'Library' / 'LaunchAgents' / f'{LAUNCHD_LABEL}.plist'


def launchd_label() -> str:
    return LAUNCHD_LABEL


def launchd_domain() -> str:
    return f'gui/{os.getuid()}'


def launchd_service_target() -> str:
    return f'{launchd_domain()}/{launchd_label()}'


def launchd_disabled() -> bool | None:
    result = subprocess.run(['launchctl', 'print-disabled', launchd_domain()], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    if result.returncode != 0:
        return None
    pattern = rf'"{re.escape(launchd_label())}"\s*=>\s*(true|false)'
    match = re.search(pattern, result.stdout or '')
    if not match:
        return False
    return match.group(1) == 'true'


def service_status_text() -> str:
    system = os_name()
    if system == 'Darwin':
        result = subprocess.run(['launchctl', 'list', launchd_label()], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        return 'service: running' if result.returncode == 0 else 'service: stopped'
    if system in {'Linux', 'Windows'}:
        return f'service: unsupported OS {system} (TODO)'
    return f'service: unsupported OS {system}'


def backend_autostart_status_text(system: str | None = None) -> str:
    system = system or os_name()
    if system == 'Darwin':
        plist = launchd_plist_path()
        if not plist.exists():
            return 'backend autostart: not installed'
        disabled = launchd_disabled()
        service_state = service_status_text().split(':', 1)[1].strip()
        if disabled is True:
            return f'backend autostart: disabled ({service_state})'
        if disabled is False:
            return f'backend autostart: enabled ({service_state})'
        return f'backend autostart: unknown ({service_state})'
    if system in {'Linux', 'Windows'}:
        return f'backend autostart: unsupported OS {system} (TODO)'
    return f'backend autostart: unsupported OS {system}'


def backend_autostart_cmd(action: str, system: str | None = None) -> list[str]:
    system = system or os_name()
    if system != 'Darwin':
        raise SystemExit(f'Backend autostart {action} is TODO on {system}; macOS is supported first.')
    if not launchd_plist_path().exists():
        raise SystemExit('Service is not installed. Run ./install.sh from the repo first.')
    if action == 'enable':
        return ['launchctl', 'enable', launchd_service_target()]
    if action == 'disable':
        return ['launchctl', 'disable', launchd_service_target()]
    raise SystemExit(f'Unknown backend autostart action: {action}')


def require_service_installed() -> None:
    system = os_name()
    if system == 'Darwin':
        if not launchd_plist_path().exists():
            raise SystemExit('Service is not installed. Run ./install.sh from the repo first.')
        return
    if system in {'Linux', 'Windows'}:
        raise SystemExit(f'Service control is TODO on {system}; macOS is supported first.')
    raise SystemExit(f'Unsupported OS: {system}')


def service_cmd(action: str) -> int:
    system = os_name()
    require_service_installed()
    if system != 'Darwin':
        raise SystemExit(f'Service control is TODO on {system}; macOS is supported first.')

    plist = str(launchd_plist_path())
    if action == 'start':
        result = subprocess.run(['launchctl', 'load', plist], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    elif action == 'stop':
        result = subprocess.run(['launchctl', 'unload', plist], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    elif action == 'restart':
        subprocess.run(['launchctl', 'unload', plist], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        result = subprocess.run(['launchctl', 'load', plist], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    else:
        raise SystemExit(f'Unknown service action: {action}')
    if result.stdout:
        print(result.stdout, end='')
    if result.returncode == 0:
        print(service_status_text())
    return int(result.returncode)


def launchd_plist(config_path: Path, program: Path, label: str = LAUNCHD_LABEL) -> str:
    log = DEFAULT_LOG_DIR / 'launchd-daemon.log'
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{label}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{program}</string>
    <string>--config</string>
    <string>{config_path}</string>
    <string>daemon</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
  </dict>
</dict>
</plist>
'''


def install_script(generated_dir: Path) -> str:
    return f'''#!/bin/sh
set -eu
GENERATED_DIR="{generated_dir}"
case "$(uname -s)" in
  Darwin)
    mkdir -p "$HOME/Library/LaunchAgents"
    cp "$GENERATED_DIR/{LAUNCHD_LABEL}.plist" "$HOME/Library/LaunchAgents/{LAUNCHD_LABEL}.plist"
    launchctl enable "gui/$(id -u)/{LAUNCHD_LABEL}" 2>/dev/null || true
    ;;
  Linux)
    echo "Linux service install is TODO; macOS is supported first." >&2
    exit 1
    ;;
  *)
    echo "Unsupported OS: $(uname -s)" >&2
    exit 1
    ;;
esac
'''
