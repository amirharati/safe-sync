# Tauri Tray Workflow

This document is the working map for adding the Safe Sync tray UI without turning the repo into a mystery pile of generated files.

## Mental Model

Safe Sync will have two parts:

```text
safe-sync CLI/daemon   owns sync, config, status, services
Tauri tray app         shows status and calls safe-sync commands
```

The tray app is not a daemon replacement. It should be safe to quit the tray while the backend daemon keeps running.

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
- Linux returns a clear TODO/unsupported message for now.
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
- A small status window can open from the tray for diagnostics.

Current files to review:

- `ui/package.json` for npm scripts and JS dependencies.
- `ui/package-lock.json` for reproducible npm installs.
- `ui/src-tauri/Cargo.toml` for Rust/Tauri dependencies.
- `ui/src-tauri/Cargo.lock` for reproducible Rust dependency resolution.
- `ui/src-tauri/tauri.conf.json` for app identity and hidden status window config.
- `ui/src-tauri/src/lib.rs` for tray menu setup.
- `ui/src/main.ts`, `ui/index.html`, and `ui/src/styles.css` for the placeholder status window.

Review point: inspect the generated `ui/` structure together before wiring real Safe Sync commands.

### Checkpoint 3: Tray Reads Status

Goal: tray shows real Safe Sync health.

Current behavior:

- Rust/Tauri runs `safe-sync status` and parses the JSON response.
- The tray label maps `service_state` plus `sync_state.state` to simple labels such as stopped, watching, syncing, backoff, cooldown, and error.
- The status window can refresh the same status through a Tauri command.
- The status window shows health reason, backend service state, sync state, daemon seen time, and log path.

Review point: verify the labels feel clear before adding richer history or settings.

### Checkpoint 4: Tray Controls Backend

Goal: tray menu controls the existing backend safely.

Current behavior:

- Start Backend -> `safe-sync start`
- Stop Backend -> `safe-sync stop`
- Refresh Status -> `safe-sync status`
- Show Status Window -> opens the hidden diagnostics window
- Quit Tray -> quits the tray app only

The status window also exposes Start, Stop, and Refresh buttons through the same Tauri command bridge. These actions do not perform sync logic directly; they only call the existing CLI.

Still pending for later checkpoints:

- Tray icon state variants

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
- Keep Linux behavior conservative: right-click menu is the reliable baseline.

## Open Questions

- Should the tray icon use color, badge text, or monochrome variants for macOS menu bar style?
- How should Windows service/autostart be implemented later?
