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
- Corresponding whole folders or granular subfolders can detect peer changes
  automatically while every cross-computer merge remains user-initiated.

The authoritative proposal for the next cross-computer milestone is
[Linked Folders, Safe Transfer, and Recovery Design](linked-folder-transfer-design.md).
The required observability gate before broad dogfooding is
[Event Logging and Audit Design](event-logging-and-audit-design.md).

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
- Uses Dropbox's plan-bounded version/deleted-file history instead of
  app-owned remote trash or snapshots.
- Excludes dependencies/build/cache artifacts.
- Includes source, data, model artifacts, configs, lockfiles, notebooks, and logs.

### Pull From Another Machine

Compare and receive a path from another machine's backup into a local
destination.

Default behavior:

- Stages remote data on the destination filesystem before apply.
- Produces a real source/destination diff and rechecks it before apply.
- Does not delete or overwrite local destination files without explicit
  approval and a recoverable checkpoint.
- Moves verified staged files into place rather than downloading/copying them
  twice.

### Linked Folders

Manually associate a complete folder or granular subfolder with the
corresponding backup scope from another computer.

Default behavior:

- Local and peer changes are detected automatically.
- A stored common baseline distinguishes one-sided changes from conflicts.
- The user always opens Review & Sync and approves the actual merge.
- Each computer continues writing only its own remote backup tree.

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
