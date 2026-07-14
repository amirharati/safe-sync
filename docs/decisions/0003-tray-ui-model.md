# Decision 0003: Tray UI Model

## Status

Accepted

## Context

Safe Sync now has a working CLI, daemon, multi-folder configuration, machine registry, status JSON, and service install path. The next user-facing problem is visibility: the user should be able to glance at the desktop tray/menu bar and know whether sync is running, syncing, stopped, stale, or failing.

The UI must not become a second sync engine. The backend must keep working if the UI is closed, crashes, or is not installed.

## Decision

Build the first UI as a small Tauri v2 tray app.

The tray UI is an observer and controller:

- Read `safe-sync status` or the status JSON.
- Show current health in a tray icon/menu.
- Run existing CLI commands for actions such as start, stop, backup now, and logs.
- Open a small details/settings window later, after the tray behavior is proven.

The backend daemon remains separately owned by the operating system service layer:

- macOS: launchd user service.
- Linux: systemd user service.
- Windows: TODO, likely Task Scheduler or Windows service wrapper later.

Autostart is split into two independent settings:

- Backend daemon autostart: controlled by Safe Sync CLI/service commands.
- Tray UI autostart: controlled by the Tauri app autostart plugin.

The tray may self-heal the backend by calling `safe-sync start` when backend autostart is enabled but the daemon is stopped or stale. The daemon must not depend on the tray process to run.

## Why Tauri

Tauri is a good fit because:

- It supports macOS, Linux, and Windows desktop apps.
- It has system tray/menu support.
- It has an autostart plugin.
- It is lighter than Electron.
- It gives us a path from tray-only now to a real settings/details window later.

## Phase 1 Tray Scope

The first UI should include only:

- Tray icon state: ok, syncing, error, stopped/stale/unknown.
- Menu labels for health, last success, last error, and current folders count.
- Actions: Start daemon, Stop daemon, Backup now, Open logs, Refresh, Quit.
- Toggle: start tray at login.
- Toggle/status: backend daemon autostart, once CLI support exists.

It should not include:

- File browser.
- Conflict browser.
- Cross-machine transfer UI.
- Full settings editor.
- Custom sync logic.

## Backend Work Needed First

Before the tray can be clean, add backend CLI support for daemon autostart state:

```bash
safe-sync autostart backend status
safe-sync autostart backend enable
safe-sync autostart backend disable
```

The UI should call these commands instead of knowing launchd/systemd details itself.

## Consequences

Benefits:

- Sync remains robust without the UI.
- The UI is small and understandable.
- The future settings page can reuse the same backend commands.
- macOS/Linux are supported first, with Windows left as an explicit TODO.

Tradeoffs:

- There are two autostart toggles to explain.
- Packaging has a second toolchain: Rust/Tauri plus the existing Python CLI.
- Linux tray behavior varies by desktop environment, so the right-click menu path must be treated as the reliable baseline.
