# Safe Sync

Safe Sync is a small wrapper around rclone for personal multi-computer file backup and selective transfer.

The goal is not to build a new sync engine. The goal is to make a boring, inspectable workflow that backs up each computer to its own Dropbox folder and lets files be pulled between computers intentionally.

## Core Idea

- Each computer owns one machine identity and one or more remote backup folders.
- Automatic jobs are mostly one-way backup from each configured local folder to that computer's remote folders.
- Cross-computer sharing is selective: discover another computer, then pull or copy a file/folder when needed.
- Each computer publishes its own registry file at `.registry/computers/<machine_id>.json`; no shared registry file is edited by multiple machines.
- Deletes in owned backup folders are allowed only with recoverable trash.
- No tool syncs `.git/` internals.
- Build artifacts, dependency folders, and caches are ignored.
- Data, trained models, notebooks, configs, lockfiles, and experiment results are backed up.
- Metadata preservation is opt-in to avoid needless Dropbox rewrites and rate-limit pressure.

## Docs

- [Product Plan](docs/product/product-plan.md)
- [Roadmap](docs/roadmap.md)
- [Operating Model](docs/operations/operating-model.md)
- [Daemon Design](docs/operations/daemon-design.md)
- [Test Plan](docs/operations/test-plan.md)
- [Dogfood Report](docs/operations/dogfood-report.md)
- [Tauri Tray Workflow](docs/operations/tauri-tray-workflow.md)
- [Decisions](docs/decisions/0001-safe-sync-model.md)

## First Test Folder

Initial development and testing uses:

```text
~/test_sync
```

Do not point early tests at `~/projects`.


## Code Layout

```text
bin/safe-sync                 Thin executable launcher only
src/safe_sync/cli.py          CLI commands and rclone guardrails
src/safe_sync/daemon.py       Polling watch daemon state and scan helpers
src/safe_sync/path_filter.py  Watch-event ignore helper
src/safe_sync/service.py      macOS service install/control rendering
ui/                           Planned Tauri tray app workspace
tests/                        Unit tests for daemon state behavior
```

Run the CLI through `bin/safe-sync`; edit implementation code under `src/safe_sync/`.


## macOS Quickstart

Safe Sync is macOS-first right now. Linux and Windows service install are explicit TODOs.

From a downloaded/cloned repo:

```bash
cd ~/projects/safe-sync
./install.sh
```

This does four things:

1. Creates `~/.safe-sync/config.json` if it does not exist.
2. Installs the single `safe-sync` command into `/usr/local/bin` when writable, otherwise `~/.local/bin`.
3. Renders the macOS LaunchAgent from `src/safe_sync/service.py`.
4. Installs the LaunchAgent at `~/Library/LaunchAgents/com.safe-sync.daemon.plist`.

It does not start the daemon. Start it explicitly:

```bash
safe-sync start
```

Check health:

```bash
safe-sync status
safe-sync logs
```

Stop it:

```bash
safe-sync stop
```

Restart it after config changes:

```bash
safe-sync restart
```

Control backend login autostart:

```bash
safe-sync autostart backend status
safe-sync autostart backend enable
safe-sync autostart backend disable
```

Typical healthy macOS states look like:

```text
backend autostart: enabled (running)
backend autostart: enabled (stopped)
backend autostart: disabled (stopped)
```

`enabled` means launchd is allowed to start Safe Sync at login. `running` or `stopped` is the current daemon process state.

## Configuration

The local config lives at:

```text
~/.safe-sync/config.json
```

List configured folders:

```bash
safe-sync folders list
```

Add another local folder to this machine's backup set:

```bash
safe-sync folders add data ~/data_to_backup --label Data
```

Run health check:

```bash
safe-sync doctor
```

Dry-run backup for all enabled folders:

```bash
safe-sync backup --dry-run
```

Dry-run backup for one folder:

```bash
safe-sync backup test_sync --dry-run
```

Run a real backup:

```bash
safe-sync backup
```

List known computers from the remote registry:

```bash
safe-sync computers
```

Migrate an older config, if needed:

```bash
safe-sync migrate-config
```

## Install Internals

Service templates are rendered from `src/safe_sync/service.py`; generated launchd/systemd files are not kept in the repo.

The current macOS installer writes only:

```text
~/Library/LaunchAgents/com.safe-sync.daemon.plist
```

Linux and Windows service install/control remain TODO/backlog.

## Test Folder Reminder

Initial development and manual testing should still use `~/test_sync` or another small explicit folder. Do not point Safe Sync at broad folders like `~` or `~/projects` until the folder-specific config is intentionally reviewed.
