# Daemon Design

The daemon is a thin orchestrator around the existing `safe-sync backup` behavior. It reads the same config from `~/.safe-sync/config.json`.

It should not implement sync logic. It should decide when to call the backup command for the enabled configured folders.

## Inputs

- Enabled folder list from config.
- Local path and filter file for each folder.
- Debounce/cooldown/backoff settings from config.
- File watcher events.
- Fallback timer ticks.
- Previous backup result.

## Outputs

- Calls the existing backup code.
- Updates status JSON.
- Appends to the normal log.

## Event Coalescing

The daemon stores a set of dirty configured-folder IDs:

```text
dirty_folders = {"folder-a", "folder-c"}
```

It does not need a full queue of changed paths. Native events are coalesced by
configured folder; rclone scans each dirty folder and decides what changed.
Watcher-triggered work does not rescan unrelated folders. A currently running
rclone process is not interrupted: events remain in the native watcher queue
and their folder IDs are scheduled as the next targeted cycle. The configured
startup and fallback full-profile backups remain the reconciliation paths for
events missed during sleep, restart, or watcher failure.

## Ignore Policy

Watcher events inside known generated folders should be ignored early:

The watcher filter mirrors the directory and file exclusions in
`config/filter.txt`, including VCS internals, dependencies, build outputs,
caches, and compiled intermediates. A watched root named `dist` remains valid;
filtering is applied only to paths relative to that root.

This keeps noisy build systems from waking the daemon constantly.

## Manual Backup

Manual command remains:

```bash
safe-sync backup
```

Manual backup should bypass watcher debounce, but it should still respect locks and rate-limit backoff unless a later explicit `--force` option is added.

## Testing Plan

Initial daemon testing should be against:

```text
~/safe-sync-test
```

The first daemon test should run in dry-run mode:

```bash
safe-sync daemon --dry-run --once --poll-interval 2 --debounce 5
```

No automatic daemon should be installed until this behavior is reviewed.



## Implementation status

The installed daemon uses the app-managed `watchdog` runtime: FSEvents on
macOS and inotify on Linux. The backend never repeatedly snapshots a broad
tree in normal native mode. Full-tree polling remains a visible degraded mode
when the native backend cannot start.

Loop behavior:

1. Subscribe to every enabled local folder through the native event backend.
2. Ignore generated paths before they mark a folder dirty.
3. Coalesce relevant events by folder.
4. Wait for the debounce window to be quiet.
5. Run normal guarded backups for the dirty folders only. With no older durable
   cycle pending, startup, Backup Now, and the fallback timer deliberately
   reconcile all enabled folders.
6. Keep retries and pending generation publication durable and ahead of newly
   dirty work; do not interrupt an active rclone child to reorder it.
7. Refresh this machine's registry file after successful real backups.
8. Respect a minimum interval between runs.
9. Enter backoff when rclone output indicates Dropbox rate limiting or registry update failure.
10. Run a fallback reconciliation backup after the fallback interval even if no event was noticed.

Service install is handled by the repo installer:

```bash
./install.sh
```

It installs the `safe-sync` command plus the app-managed watcher, renders and
installs the macOS LaunchAgent or Linux systemd user service, and starts the
daemon when setup has an enabled folder. Runtime config stays in
`~/.safe-sync`. Windows service support remains deferred.


## Install workflow

The repo is the install unit:

```bash
cd /path/to/safe-sync
./install.sh
```

Use `safe-sync start` to start the installed macOS LaunchAgent and `safe-sync stop` to stop it. Use `safe-sync autostart backend status` to inspect login autostart state.
