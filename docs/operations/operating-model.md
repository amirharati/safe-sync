# Operating Model

## Remote Layout

```text
dropbox:computer-backups/
  macbook/
    projects/
  linuxbox/
    projects/
  windowsbox/
    projects/
  shared/
  .trash/
    macbook/
    linuxbox/
    windowsbox/
```

## Backup

Each computer backs up its local project folder to its own remote folder.

Example for Mac:

```bash
rclone sync ~/projects dropbox:computer-backups/macbook/projects \
  --filter-from ~/.safe-sync/filter.txt \
  --backup-dir dropbox:computer-backups/.trash/macbook/$(date +%Y-%m-%dT%H-%M-%S)
```

This means local deletes can be reflected in that machine's backup, but the old remote file is moved to trash first.

## Pull

Pull a folder from Linux backup to Mac:

```bash
rclone copy dropbox:computer-backups/linuxbox/projects/my_exp ~/projects/from-linux/my_exp \
  --filter-from ~/.safe-sync/filter.txt
```

This does not delete local files.

## Trash Path

Trash preserves original relative paths:

```text
dropbox:computer-backups/.trash/macbook/2026-07-12T18-45-00/labs/mnist/data/train.csv
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

