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

- App builds/runs locally.
- No real sync actions yet.
- A placeholder tray menu appears.
- A simple window can open for diagnostics if needed.

Review point: inspect `ui/package.json`, `ui/src-tauri/tauri.conf.json`, and `ui/src-tauri/src/` together.

### Checkpoint 3: Tray Reads Status

Goal: tray shows real Safe Sync health.

Expected behavior:

- Poll `safe-sync status` or read status JSON every few seconds.
- Map status to simple UI states:
  - ok
  - syncing
  - stopped
  - stale
  - error
  - unknown
- Menu shows last success/error and log path.

Review point: verify status mapping before adding controls.

### Checkpoint 4: Tray Controls Backend

Goal: tray menu controls the existing backend safely.

Menu actions:

- Start daemon -> `safe-sync start`
- Stop daemon -> `safe-sync stop`
- Backup now -> `safe-sync backup`
- Open logs -> open current log path
- Refresh -> re-read status
- Quit -> quit tray only

Review point: confirm no action deletes files or bypasses backend guardrails.

### Checkpoint 5: Autostart Toggles

Goal: expose login behavior clearly.

Two separate settings:

- Start Safe Sync daemon at login.
- Start tray app at login.

Implementation split:

- Backend daemon autostart uses `safe-sync autostart backend ...`.
- Tray autostart uses Tauri's autostart plugin.

Review point: make sure disabling the tray does not disable backend sync unless explicitly requested.

### Checkpoint 6: Package and Install

Goal: make install understandable.

Install should explain:

- `./install.sh` installs the backend command/service definition.
- Tauri build installs the tray app.
- Starting/stopping the daemon remains available through `safe-sync` even without the UI.

Review point: decide whether repo `install.sh` should eventually call the UI installer or keep UI packaging separate.

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

- Should `Backup Now` run all enabled folders or prompt/select later? Default should be all enabled folders for phase 1.
- Should opening logs use the OS opener directly from Tauri, or should `safe-sync logs` remain the first phase behavior?
- Should the tray icon use color, badge text, or monochrome variants for macOS menu bar style?
- How should Windows service/autostart be implemented later?
