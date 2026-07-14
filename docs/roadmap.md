# Roadmap

## Phase 0: Docs and Existing Setup

- Capture workflow decisions.
- Define safe defaults.
- Keep current rclone remotes and filters available for reference.
- Do not build a tray UI yet.

## Phase 1: CLI Wrapper

Build a small `safe-sync` command that can:

- Show config.
- Run backup for this machine.
- Run backup in dry-run mode.
- Pull a file/folder from another machine backup.
- List known machines.
- Print status and latest log path.

Commands:

```bash
safe-sync backup
safe-sync backup --dry-run
safe-sync pull <machine> <remote-path> <local-path>
safe-sync list <machine> <path>
safe-sync status
```

Initial implementation must target `~/safe-sync-test` only. Do not run against a broad or important work folder until the test folder workflow is proven.

## Phase 2: Watch Daemon Skeleton

Add a daemon structure and render install files, but do not auto-install it yet.

- Add watcher state machine docs.
- Add config fields for debounce/cooldown/fallback/backoff.
- Add placeholder modules for watch orchestration.
- Keep `~/safe-sync-test` as the only target.
- Review design before full implementation.

## Phase 3: Watch Daemon Implementation

Implement watcher-first backup triggering:

- Watch local folder for changes.
- Debounce noisy changes.
- Coalesce changes while a backup is running.
- Respect Dropbox rate-limit backoff.
- Run fallback timer periodically.
- Support dry-run daemon testing. Basic polling daemon is implemented first; native filesystem events can be added later.

## Phase 4: Scheduler

Add install helpers:

- Repo-level `./install.sh` command for service install and `safe-sync start` / `safe-sync stop` commands for daemon control.
- Linux service/autostart controls and Windows Task Scheduler later if needed.

The scheduler runs the watch daemon, not raw backup, after the watch daemon is reviewed.

## Phase 5: Status File and Logs

Write machine-readable status:

```json
{
  "state": "idle",
  "last_start": null,
  "last_success": null,
  "last_error": null,
  "last_command": null
}
```

Keep human logs in a stable path.

## Phase 6: Tray Status UI

Build a thin Tauri tray/menu app:

- Read `safe-sync status` or status JSON.
- Show ok/syncing/stopped/stale/error.
- Menu actions for start daemon, stop daemon, backup now, open logs, refresh, and quit tray.
- Keep sync logic in the existing CLI/daemon only.
- Split autostart into backend daemon autostart and tray UI autostart.
- Add backend autostart CLI commands before the tray depends on them. macOS is first; Linux/Windows remain backlog.

Work through documented checkpoints in `docs/operations/tauri-tray-workflow.md`.

## Not Planned Initially

- Custom sync engine.
- Making the daemon depend on the tray UI.
- Complex conflict browser.
- Automatic multi-way live sync for all projects.
- Editing rclone internals.


Windows service support remains TODO.
