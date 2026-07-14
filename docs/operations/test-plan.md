# Test Plan

## Test Folder

Use this local folder for initial testing:

```text
~/safe-sync-test
```

Do not use a broad or important work folder during early implementation.

## Test Remote

Use a disposable remote path:

```text
dropbox:computer-backups/test/macbook/safe-sync-test
```

Trash for this test should go under:

```text
dropbox:computer-backups/.trash/test/macbook/<timestamp>
```

## Seed Files

The test tree includes files that should be backed up:

```text
README.md
src/app.py
data/results.csv
models/model.pt
```

It also includes files that should be ignored by the filter:

```text
node_modules/pkg/index.js
.venv/lib/site.py
dist/bundle.js
```

## First Commands

Dry run backup:

```bash
rclone sync ~/safe-sync-test dropbox:computer-backups/test/macbook/safe-sync-test \
  --filter-from /path/to/safe-sync/config/filter.txt \
  --backup-dir dropbox:computer-backups/.trash/test/macbook/DRY-RUN \
  --dry-run
```

Expected:

- Includes `README.md`, `src/app.py`, `data/results.csv`, `models/model.pt`.
- Excludes `node_modules/`, `.venv/`, and `dist/`.
- Does not touch broad or important work folders.

Real test backup should only happen after dry-run output is reviewed.

## Current Dropbox Backoff Behavior

During the first real test on 2026-07-12, Dropbox returned:

```text
too_many_requests
Trying again in 300 seconds
```

Safe Sync now has an outer command timeout and rclone max-duration settings so a daemon run will fail visibly instead of waiting forever.

If this happens during testing, wait a few minutes and retry. Do not keep hammering Dropbox in a loop.
