# Tauri Tray Workflow

This document is the working map for adding the Safe Sync tray UI without turning the repo into a mystery pile of generated files.

## Mental Model

Safe Sync will have two parts:

```text
safe-sync CLI/daemon   owns sync, config, status, services
Tauri tray app         shows status and calls safe-sync commands
```

The tray app is not a daemon replacement. It should be safe to quit the tray while the backend daemon keeps running.

On macOS, the small tray surface is a borderless native `NSWindow` containing the existing Tauri WebView. It is positioned from the status item's native AppKit window coordinates, avoiding the unreliable first-show behavior of Tauri's cross-platform `set_position` path. The larger control panel remains a normal Tauri window.

The small AppKit bridge lives directly in `ui/src-tauri/src/lib.rs`; there is no separate popover plugin. Linux and Windows keep a basic hidden-window fallback until their tray behavior is implemented and tested on those platforms.

## Expected Repo Layout

After scaffolding, the repo should look roughly like this:

```text
bin/safe-sync                    Existing Python launcher
src/safe_sync/                   Existing backend code
ui/                              Tauri app workspace
ui/package.json                  Frontend/package scripts
ui/src/                          Tray frontend code
ui/src-tauri/                    Rust/Tauri backend for desktop shell
ui/src-tauri/tauri.conf.json     Tauri app config
ui/src-tauri/src/                Rust commands/tray setup
```

Service files are generated at install time from `src/safe_sync/service.py`. Do not keep generated launchd/systemd files in the repo.

Keep generated build output out of git:

```text
ui/node_modules/
ui/dist/
ui/src-tauri/target/
```

## Checkpoints

### Checkpoint 1: Backend Autostart CLI

Goal: make backend service autostart controllable through `safe-sync`, not through UI-specific OS code.

Commands to add:

```bash
safe-sync autostart backend status
safe-sync autostart backend enable
safe-sync autostart backend disable
```

Expected behavior:

- macOS uses the existing launchd service definition plus `launchctl enable/disable` for persistent autostart state.
- Linux uses a `systemd --user` service named `safe-sync-daemon.service`.
- Windows returns a clear TODO/unsupported message for now.
- Tests cover macOS command routing and unsupported-platform behavior.

Current macOS output shape:

```text
backend autostart: enabled (running)
backend autostart: enabled (stopped)
backend autostart: not installed
```

Review point: confirm command names and output before UI calls them.

### Checkpoint 2: Tauri Skeleton

Goal: create the smallest Tauri app under `ui/` and understand the generated structure.

Expected behavior:

- App builds locally with `npm run build`.
- Rust/Tauri checks with `cd ui/src-tauri && cargo check`.
- No real sync actions yet.
- A placeholder tray menu appears when run with `npm run tauri dev`.
- A control panel window can open from the tray for diagnostics and settings.

Current files to review:

- `ui/package.json` for npm scripts and JS dependencies.
- `ui/package-lock.json` for reproducible npm installs.
- `ui/src-tauri/Cargo.toml` for Rust/Tauri dependencies.
- `ui/src-tauri/Cargo.lock` for reproducible Rust dependency resolution.
- `ui/src-tauri/tauri.conf.json` for app identity and hidden control panel window config.
- `ui/src-tauri/src/lib.rs` for tray menu setup.
- `ui/src/main.ts`, `ui/index.html`, and `ui/src/styles.css` for the control panel window.

Review point: inspect the generated `ui/` structure together before wiring real Safe Sync commands.

### Checkpoint 3: Tray Reads Status

Goal: tray shows real Safe Sync health.

Current behavior:

- Rust/Tauri runs `safe-sync status` and parses the JSON response.
- The macOS tray click opens a persistent borderless panel with status and controls.
- AppKit supplies the status-item anchor and screen bounds before the panel is shown.
- The tray label maps `service_state` plus `sync_state.state` to simple labels such as stopped, watching, syncing, backoff, cooldown, and error.
- The control panel can refresh the same status through a Tauri command.
- The control panel shows health reason, backend service state, sync state, daemon seen time, and log path.

Review point: verify the labels feel clear before adding richer history or settings.

### Checkpoint 4: Tray Controls Backend

Goal: tray menu controls the existing backend safely.

Current behavior:

- Start Backend -> `safe-sync start`
- Stop Backend -> `safe-sync stop`
- Refresh Status -> `safe-sync status`
- Open Control Panel -> opens the hidden control panel window
- Quit Tray -> quits the tray app only

The control panel also exposes Start, Stop, and Refresh buttons through the same Tauri command bridge. These actions do not perform sync logic directly; they only call the existing CLI.

The tray icon switches to a high-contrast red-badged variant whenever Safe
Sync reports `health: error`, including reconnect-required Dropbox failures.
It returns to the normal icon when the error clears.

Review point: confirm no action deletes files or bypasses backend guardrails.

### Checkpoint 5: Autostart Toggles

Goal: expose login behavior clearly.

Current behavior:

- Backend daemon autostart uses `safe-sync autostart backend ...`.
- Tray app autostart is installed by `./install.sh` as `~/Library/LaunchAgents/com.safe-sync.tray.plist`.
- The tray app can be quit without stopping the backend daemon.

Still pending for later UI:

- In-app toggles for backend-at-login and tray-at-login.
- Linux and Windows autostart implementations.

Review point: make sure disabling the tray does not disable backend sync unless explicitly requested.

### Checkpoint 6: Package and Install

Goal: make install understandable.

Current behavior:

- `./install.sh` installs the backend command/service definition.
- `./install.sh` runs `npm ci` and `npm run tauri build` for the tray app unless `SAFE_SYNC_INSTALL_UI=0` is set.
- The built macOS app is copied to `~/Applications/Safe Sync.app` by default.
- The tray LaunchAgent is installed at `~/Library/LaunchAgents/com.safe-sync.tray.plist`.
- Starting/stopping the daemon remains available through `safe-sync` even without the UI.

Review point: test login/startup behavior after a normal install.

## Tray Menu Draft

```text
Safe Sync: OK
Last sync: 2026-07-13 18:06
Folders: 2

Start Daemon
Stop Daemon
Backup Now
Open Logs

Start Backend at Login: On
Start Tray at Login: On

Quit Tray
```

When there is an error, put the error near the top and keep actions available:

```text
Safe Sync: Error
Remote preflight failed

Start Daemon
Backup Now
Open Logs
Quit Tray
```

## Design Rules

- The tray is a status/control surface, not the source of truth.
- Every sync action goes through the existing CLI.
- Avoid background writes from the UI except explicit start/stop/autostart actions.
- Prefer small, inspectable commands over hidden behavior.
- Do not position the macOS quick tray surface with Tauri's `set_position`; use the native status-item and `NSWindow` frames in the same AppKit coordinate system.
- Keep Linux behavior conservative: right-click menu is the reliable baseline.

## Open Questions

- Should the tray icon use color, badge text, or monochrome variants for macOS menu bar style?
- How should Windows service/autostart be implemented later?
