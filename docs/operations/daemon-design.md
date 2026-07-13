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

The daemon stores a simple dirty flag:

```text
dirty = true
```

It does not need a full queue of changed paths for the first implementation. Rclone scans the configured folders and decides what changed. The daemon only records which folder snapshots changed so status can say what woke it up.

## Ignore Policy

Watcher events inside known generated folders should be ignored early:

```text
node_modules
.venv
venv
dist
build
out
.cache
.git
```

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
~/test_sync
```

The first daemon test should run in dry-run mode:

```bash
safe-sync daemon --dry-run --once --poll-interval 2 --debounce 5
```

No automatic daemon should be installed until this behavior is reviewed.



## Implementation status

The first working daemon uses dependency-free polling so it works on macOS, Linux, and Windows-style Python environments without installing a native watcher package.

Loop behavior:

1. Snapshot every enabled configured local folder.
2. Ignore generated paths before comparing snapshots.
3. Mark the daemon dirty when any folder snapshot changes.
4. Wait for the debounce window to be quiet.
5. Run normal guarded backups for all enabled folders.
6. Refresh this machine's registry file after successful real backups.
7. Respect a minimum interval between runs.
8. Enter backoff when rclone output indicates Dropbox rate limiting or registry update failure.
9. Run a fallback backup after the fallback interval even if no change was noticed.

Service install is handled by the repo installer:

```bash
./install.sh
```

It installs the single `safe-sync` command, renders OS service definitions in a temporary directory, installs them, then removes the temporary files. It does not start the daemon. Runtime config stays in `~/.safe-sync`.


## Install workflow

The repo is the install unit:

```bash
cd ~/projects/safe-sync
./install.sh
```

Use `safe-sync start` to start the installed OS service and `safe-sync stop` to stop it.
