# Product Plan

## Problem

Personal project files need to be backed up and selectively moved between Mac, Linux, and possibly Windows. Existing tray wrappers are too opaque and fragile, and broad two-way sync is hard to trust.

## Goal

Make a boring, inspectable personal backup and transfer tool using rclone.

Success means:

- Each machine reliably backs up its project folder.
- Files can be pulled from another machine when needed.
- Deletes are recoverable.
- Data, models, notebooks, configs, and real work are preserved.
- Build artifacts and dependency/cache folders are ignored.
- The user can understand what the tool does by reading small scripts and docs.
- The user can see sync health from a small tray/menu bar UI without trusting a hidden black box.

## Non-Goals

- Replace rclone.
- Replace Git.
- Build a large desktop product.
- Guarantee live two-way sync across all machines.
- Hide behavior behind a complicated tray app.
- Make the backend daemon depend on the tray app being open.

## Personas

Primary user:

- Works across Mac and Linux.
- Runs experiments on Linux.
- Wants results/data/models available on Mac.
- Uses Git for code but also has non-Git files worth preserving.

## Core Workflows

### Backup This Machine

Upload/mirror local project folder to this machine's owned Dropbox backup folder.

Default behavior:

- Uses `rclone sync`.
- Uses remote trash via `--backup-dir`.
- Excludes dependencies/build/cache artifacts.
- Includes source, data, model artifacts, configs, lockfiles, notebooks, and logs.

### Pull From Another Machine

Copy a path from another machine's backup into a local destination.

Default behavior:

- Uses `rclone copy`.
- Does not delete local destination files.
- Does not overwrite silently if avoidable; conflict policy should preserve both versions.

### Shared Handoff

Optional shared area for intentional transfer:

```text
dropbox:computer-backups/shared
```

This is for files that are explicitly meant to move between machines outside normal backups.


### Tray Status

Show Safe Sync health from the desktop tray/menu bar.

Default behavior:

- Reads existing backend status.
- Shows whether the daemon is running, syncing, stopped, stale, or failing.
- Offers explicit actions for start, stop, backup now, logs, and quit tray.
- Keeps backend daemon autostart separate from tray UI autostart.

## Safety Principles

- Prefer recoverable trash over permanent delete.
- Prefer copy over destructive sync for cross-machine transfer.
- Keep trash outside Git repositories.
- Preserve original relative paths in trash.
- Fail visibly rather than guess.

