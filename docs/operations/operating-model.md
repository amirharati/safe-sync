# Operating Model

## Remote Layout

```text
dropbox:computer-backups/
  <machine_id>/
    <folder_id>/
  .registry/
    computers/
      <machine_id>.json
  .trash/
    <machine_id>/
      <folder_id>/
```

## Backup

Each computer backs up each enabled local folder to that machine's owned remote folder. Use `safe-sync backup`; do not call raw `rclone sync` for normal operation.

Example shape for Mac:

```text
~/test_sync -> dropbox:computer-backups/test/<machine_id>/test_sync
```

Under the hood Safe Sync uses `rclone sync` with `--backup-dir`, so local deletes can be reflected in that machine's backup, but the old remote file is moved to trash first.

## Pull

Pull/copy from another machine backup is explicit. Use `safe-sync pull` with a full rclone source path.

```bash
safe-sync pull dropbox:computer-backups/test/linuxbox/projects/my_exp ~/projects/from-linux/my_exp
```

This uses copy semantics and does not delete local files.

## Trash Path

Trash preserves original relative paths:

```text
dropbox:computer-backups/test/.trash/macbook/test_sync/2026-07-12T18-45-00/labs/mnist/data/train.csv
```

Restore target:

```text
labs/mnist/data/train.csv
```

## Git

- Exclude `.git/`.
- Syncing working tree files is okay.
- Conflict copies may appear as untracked files.
- Do not put trash inside project repositories.

## Filter Policy

Back up:

- Source
- Docs
- Configs
- Lockfiles
- Notebooks
- Datasets
- Trained models
- Checkpoints
- Experiment results/logs

Ignore:

- `.git/`
- `node_modules/`
- `.venv/`, `venv/`
- `dist/`, `build/`, `out/`
- Runtime/build caches
- Python bytecode
- Compiled objects and libraries

