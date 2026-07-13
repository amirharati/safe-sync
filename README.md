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

## Docs

- [Product Plan](docs/product/product-plan.md)
- [Roadmap](docs/roadmap.md)
- [Operating Model](docs/operations/operating-model.md)
- [Daemon Design](docs/operations/daemon-design.md)
- [Test Plan](docs/operations/test-plan.md)
- [Dogfood Report](docs/operations/dogfood-report.md)
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
tests/                        Unit tests for daemon state behavior
```

Run the CLI through `bin/safe-sync`; edit implementation code under `src/safe_sync/`.


## Install

From a downloaded/cloned repo:

```bash
cd ~/projects/safe-sync
./install.sh
```

That installs the single `safe-sync` command, creates or keeps `~/.safe-sync/config.json`, and installs the OS service definition. It does not start the daemon.

Start the daemon:

```bash
safe-sync start
```

Stop the daemon:

```bash
safe-sync stop
```

Show daemon state and recent sync state:

```bash
safe-sync status
safe-sync logs
```

Status includes separate service and sync health fields.

## Current CLI Test Commands

The local test config lives at:

```text
~/.safe-sync/config.json
```

Run health check:

```bash
safe-sync doctor
```

List configured folders:

```bash
safe-sync folders list
```

Add another local folder to this machine's backup set:

```bash
safe-sync folders add data ~/data_to_backup --label Data
```

Dry-run backup for all enabled folders:

```bash
safe-sync backup --dry-run
```

Dry-run backup for one folder:

```bash
safe-sync backup test_sync --dry-run
```

Check status:

```bash
safe-sync status
```

List known computers from the remote registry:

```bash
safe-sync computers
```

Real backup to the disposable Dropbox test path:

```bash
safe-sync backup
```

Note: the first real backup attempt hit Dropbox `too_many_requests`, so wait before retrying real writes.


Config migration:

```bash
safe-sync migrate-config
```



Windows service install is TODO. Current installer targets macOS and Linux.
