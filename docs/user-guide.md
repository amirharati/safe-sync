# Safe Sync User Guide

This is the canonical Safe Sync help document. The repository, desktop Help
tab, and `safe-sync help` command all use this file.

Safe Sync continuously backs up selected local folders to computer-specific
folders in Dropbox. Backups are one-way from the owning computer. Copying data
from another computer is always an explicit transfer, so two computers never
silently write into the same backup folder.

## 1. Before You Install

Safe Sync currently supports macOS and Linux. Windows is not supported yet.
It installs private, checksum-verified copies of rclone and the native
filesystem watcher; do not install rclone separately.

You need the following source-build tools:

- macOS desktop: Python 3.10 through 3.13, Git, Node/npm, Rust/cargo, Xcode
  Command Line Tools, curl, and unzip.
- macOS headless: Python 3.10 through 3.13, Git, curl, unzip, and a SHA-256
  command.
- Linux desktop: Python 3.10 or newer, Git, Node/npm, Rust/cargo, curl, unzip,
  build-essential, pkg-config, WebKitGTK, AppIndicator, and librsvg packages.
- Linux headless: Python 3.10 or newer, Git, curl, unzip, and sha256sum.

Typical macOS prerequisite setup:

```bash
xcode-select --install
brew install git python node rust
```

Install Homebrew first if `brew` is unavailable. A macOS headless install does
not require Node or Rust.

Typical Ubuntu or Debian headless prerequisite setup:

```bash
sudo apt update
sudo apt install -y git python3 curl unzip
```

For the Linux desktop build, also install the native UI packages and your
normal Node and Rust toolchains:

```bash
sudo apt install -y build-essential pkg-config libwebkit2gtk-4.0-dev libayatana-appindicator3-dev librsvg2-dev
```

For a first test, use a small folder such as `~/safe-sync-test`. Do not begin
with your home directory, Dropbox directory, or a broad work tree.

## 2. Install

Clone the repository, enter it, and run the installer:

```bash
git clone <repository-url>
cd safe-sync
./install.sh
```

The normal install builds the desktop app and installs the per-user backend.
It does not require administrator access after the prerequisite packages are
present.

For a machine without a desktop UI:

```bash
./install.sh --headless
```

The command is installed at `~/.local/bin/safe-sync`. If your shell cannot
find it immediately, open a new terminal. The installer adds the directory to
your shell startup file when necessary.

To update an existing installation without replacing its configuration or
Dropbox authorization:

```bash
git pull
./install.sh --update
```

## 3. First Setup With the Desktop App

Open Safe Sync from Applications or the tray icon.

1. Open the Status tab and select Connect Dropbox.
2. Approve Dropbox access in the browser window.
3. Choose the first local folder to back up.
4. Select Finish Setup.
5. Confirm that Backend says running and the overall status is healthy.

The default remote base is `dropbox:computer-backups`. The first backup is
queued during startup. Use Backup Now when you want an immediate backup.

## 4. First Setup on a Headless Machine

On the headless machine, start the authorization handoff:

```bash
safe-sync connect-dropbox --headless
```

On a browser-equipped machine with Safe Sync installed, generate the token:

```bash
safe-sync rclone authorize dropbox
```

Copy the complete JSON token into the waiting headless prompt. Then configure
one or more existing local folders:

```bash
safe-sync setup --folder ~/safe-sync-test
safe-sync status
```

You can add several folders in one command:

```bash
safe-sync setup --folder ~/work --folder ~/data
```

Safe Sync rejects unusually broad roots by default. If you intentionally want
one, review the path first and make the exception explicit:

```bash
safe-sync setup --folder ~/projects --allow-unsafe-local-path
```

## 5. Everyday Backup Use

The per-user backend starts after login and watches enabled folders with native
filesystem events. Normal file changes are coalesced and backed up after the
debounce period. A periodic reconciliation backup covers events missed during
sleep or restart.

In the desktop app:

- Status shows backend health, sync state, every configured folder, exact
  whole-profile folder completion, current-folder progress, recent files, and
  errors.
- Backup Now queues an immediate safe backup.
- Start Backend and Stop Backend control the background service.
- Open Logs opens the current log location.
- Settings manages local folders, profiles, and timing controls.

Backup progress has three honest phases. `Scanning and comparing` discovers
the complete change set and intentionally has no percentage. `Transferring`
then shows a stable file percentage, file/byte totals, and an approximate ETA.
`Finalizing` publishes the backup's change record. `Overall backup` is derived
from the durable queue and reports completed, active, and waiting folders even
across a restart or provider cooldown. The separately labeled `Current folder
progress` shows the stable file percentage within the named active folder. It
also keeps the comparison-phase total fixed if a provider failure makes
rclone's retry denominator shrink, and reports failed files for that attempt.

Dropbox uploads use verified synchronous batching with a larger small-file
batch and transfer pool. Files remain directly mirrored and individually
inspectable in Dropbox; Safe Sync does not use rclone's asynchronous batch mode.

From a terminal:

```bash
safe-sync status
safe-sync backup
safe-sync logs
safe-sync doctor
```

`safe-sync backup` queues work in the running daemon. Use a dry run to review
what a direct backup would do:

```bash
safe-sync backup --dry-run
safe-sync backup FOLDER_ID --dry-run
```

Start, stop, or restart the backend when needed:

```bash
safe-sync start
safe-sync stop
safe-sync restart
```

## 6. Manage Backup Folders

The Settings tab lists watched folders and lets you add, edit, enable, disable,
or remove them. Removing a folder from Safe Sync does not delete its local
files or its existing Dropbox backup.

Equivalent CLI commands include:

```bash
safe-sync folders list
safe-sync folders add data ~/data --label Data
safe-sync folders update data ~/data --enabled
safe-sync folders update data ~/data --disabled
safe-sync folders remove data
```

Each enabled folder belongs to the active local profile and receives its own
remote backup path. Generated dependencies, caches, build outputs, and `.git`
internals are excluded. Source, documents, data, models, notebooks, lockfiles,
and experiment results are retained.

## 7. Compare and Safely Receive From Another Computer

Every configured computer publishes a small registry record to Dropbox. The
Backups tab displays local profiles and remote computers discovered from
those records.

Use the Receive tab to bring data from another computer:

1. Choose the remote computer and one of its published folders.
2. Browse the source and optionally select specific files or subfolders.
3. Choose any local destination or a watched-folder shortcut.
4. Leave Compare only enabled and review the true file-by-file comparison.
5. Disable Compare only to create a durable receive job. The data downloads
   into `.safe-sync-work` beside the destination; live destination files do
   not change during staging.
6. Open Jobs, choose an action for every difference, and apply it. Missing
   files may be added; a replacement or deletion always requires an explicit
   choice and checkpoints the old local version first.
7. Use Roll Back if needed. Rollback restores unchanged applied files, but if
   you edited a file after apply it preserves that edit and creates a separate
   recovered copy instead of overwriting it.

Receives never change the source computer's backup. Comparison is read-only,
staging cannot change live destination data, and apply uses destination-
adjacent same-filesystem moves. Receive, apply, rollback, and backup operations
share the daemon's single work lane. Interrupted applies are reconciled before
an affected backup can continue.

CLI examples:

```bash
safe-sync computers
safe-sync list dropbox:computer-backups/MACHINE/FOLDER --depth 2
safe-sync compare dropbox:computer-backups/MACHINE/FOLDER ~/Downloads/FOLDER
safe-sync pull dropbox:computer-backups/MACHINE/FOLDER ~/Downloads/FOLDER --dry-run
safe-sync pull dropbox:computer-backups/MACHINE/FOLDER ~/Downloads/FOLDER
safe-sync pull dropbox:computer-backups/MACHINE/FOLDER ~/Downloads/FOLDER --select report.csv --select assets/
safe-sync jobs list
safe-sync jobs show JOB_ID
safe-sync jobs apply JOB_ID --policy report.csv=replace --policy notes.txt=keep_both
safe-sync jobs reconcile JOB_ID
safe-sync jobs rollback JOB_ID
```

`safe-sync pull` is now a compatibility name for the staged receive workflow;
it never copies directly over live files. Repeat `--select` to receive several
items. Available conflict actions are `keep_local`, `keep_both`, `replace`,
`delete`, `leave_staged`, and `add` where applicable.

To clone a complete remote folder into a new or empty path, select Clone in
the Receive tab or run:

```bash
safe-sync receive dropbox:computer-backups/MACHINE/FOLDER ~/Projects/FOLDER --clone
```

A clone has a new local identity; it never adopts the peer computer's backup
ownership. Add it as a watched folder separately if desired.

## 8. Linked Folders and Change Detection

A linked folder maps one configured local folder, or a granular subfolder, to
one peer backup scope. Detection and comparison are automatic/read-only;
cross-computer changes are always reviewed and applied by the user.

Both scopes must contain the same data and use the same filter policy when the
link is created. Overlapping local links, changed peer installation IDs, and
filter-policy changes are blocked for review.

Use Linked Folders in the control panel, or:

```bash
safe-sync links add projects ubuntu-work projects --local-subpath my-cool-app --peer-subpath apps/my-cool-app --label "My Cool App"
safe-sync links list
safe-sync links status
safe-sync links status LINK_ID
safe-sync links remove LINK_ID
```

`links status` performs a three-way comparison against the last accepted
common baseline. It distinguishes local changes, peer changes, matching edits
on both sides, and real conflicts including delete-versus-modify.

## 9. History and Recovery

History lists older or deleted files retained in Safe Sync's dated remote
trash. Select Stage Recovery in the control panel, or use the CLI. Recovery
creates a normal receive job, so it is compared, reviewed, checkpointed, and
rollback-capable rather than restored directly over a local file.

```bash
safe-sync history FOLDER_ID
safe-sync history FOLDER_ID --receive TIMESTAMP/path/to/file
```

Successful backups now publish immutable changed-path generation records plus
an updated `latest.json` pointer. These support linked-folder change detection;
they are not yet advertised as complete whole-folder point-in-time snapshots.

## 10. Profiles and Settings

A profile represents a local computer identity and its folder set. Most users
need one profile per physical computer. Advanced users can create and activate
additional local profiles from Settings or the CLI:

```bash
safe-sync profiles list
safe-sync profiles add lab --label "Lab computer"
safe-sync profiles activate lab
safe-sync config show
```

Activating a profile changes which identity and folders the daemon uses. Check
the selected profile and folder list before running a backup.

Backend login startup can be inspected or changed with:

```bash
safe-sync autostart backend status
safe-sync autostart backend enable
safe-sync autostart backend disable
```

On a normal desktop or laptop, the backend starts after user login. A Linux
server that must sync before any login additionally needs user lingering,
which is an optional administrator-level system setting.

## 11. Understand Safety and Deletions

- Automatic backup is one-way: local folder to that computer's remote folder.
- A local deletion can remove the corresponding remote item, but rclone moves
  the prior remote version into Safe Sync's dated remote trash.
- Safe Sync never automatically pulls another computer's files.
- Receive staging, checkpoints, and journals are excluded from backup and
  watcher events even when an older user filter file is still installed.
- Normal uninstall and folder removal do not erase remote backups.
- Do not point two computer identities at the same owned remote folder.
- Do not watch a Dropbox-synced local directory or an entire home directory.

## 12. Activity and Audit Logging

The Activity tab uses Safe Sync's structured event journal. It correlates
watcher detections, queued work, backup starts, per-file results, generation
publication, receive jobs, apply, reconciliation, rollback, and failures.

Audit events are always recorded. The selected diagnostic level controls
additional implementation detail:

- `quiet`: audit events and diagnostic errors.
- `normal`: audit events, warnings, lifecycle summaries, and useful rclone
  summaries. This is the production default.
- `debug`: watcher/coalescing decisions, queue reasoning, rclone lifecycle and
  file-result summaries, and cloud replication decisions.
- `trace`: raw rclone debug detail and high-frequency internal transitions for
  short supervised tests. Repetitive raw output is capped per operation while
  errors and aggregate progress remain visible.

Change the persistent level or temporarily enable Debug:

```bash
safe-sync logs level normal
safe-sync logs level debug --for 2h
```

Query, filter, export, and inspect cloud replication:

```bash
safe-sync logs status
safe-sync logs show --since 2h
safe-sync logs show --event backup.path_result --folder projects
safe-sync logs show --severity error --json
safe-sync logs export --since 24h --output safe-sync-audit.jsonl
safe-sync logs cloud-status
safe-sync logs sync
```

Each profile has a crash-safe 64 MiB local circular journal by default. It
uses fixed segments and replaces the oldest segment after reaching the bound.
If offline activity wraps before reaching Dropbox, Safe Sync records the exact
missing sequence range and shows degraded audit health rather than claiming a
complete history.

Sealed segments copy automatically to the owning profile's remote base below
`.audit/<profile>/<machine>/<install>/`. They never copy to whichever profile
happens to become active later. Backup traffic has priority over log copying.

This journal records what happened; it does not contain file contents and is
not a recovery database. Remote trash, backup generations, and receive-job
checkpoints remain the recovery mechanisms. A future durable recovery-event
store will be separate from this bounded diagnostic journal.

## 13. Troubleshooting

Start with:

```bash
safe-sync status
safe-sync doctor
safe-sync logs show --since 2h
safe-sync logs show --severity error
safe-sync logs cloud-status
```

If setup is required, connect Dropbox and run setup with at least one folder.
If the backend is stopped, run `safe-sync start`. After configuration changes,
run `safe-sync restart` if it was not restarted automatically.

If Dropbox authorization was revoked or expired, reconnect it. Desktop users
can select Reconnect Dropbox. Headless users run:

```bash
safe-sync connect-dropbox --headless --reconnect
```

If Dropbox rate-limits a transfer, Safe Sync enters a visible cooldown and
keeps queued work for the next safe opportunity. Do not start a competing raw
rclone process. A recognized temporary Dropbox read or network failure keeps
the durable folder/report state, shows a bounded retry countdown, and retries
only unfinished work when that countdown expires. A normal backend stop during
backup is recorded as an interruption with recovery pending, not as corruption
or a completed backup; the next daemon start reconciles the retained report.

For Linux service details:

```bash
systemctl --user status safe-sync-daemon.service
journalctl --user -u safe-sync-daemon.service
```

For command-specific syntax, append `--help`, for example:

```bash
safe-sync pull --help
safe-sync jobs apply --help
safe-sync links add --help
safe-sync folders add --help
```

## 14. Files and Locations

- Configuration: `~/.safe-sync/config.json`
- Dropbox authorization: `~/.safe-sync/rclone.conf`
- Installed runtime: `~/.local/share/safe-sync/current/`
- Command: `~/.local/bin/safe-sync`
- Runtime status and socket: `~/.local/state/safe-sync/`
- Structured per-profile event journals:
  `~/.local/state/safe-sync/event-journal/`
- Receive-job index and local generation records:
  `~/.local/state/safe-sync/jobs/` and `generations/`
- Destination-adjacent staging and checkpoints: `.safe-sync-work/` beside the
  selected destination (retained during dogfood for recovery review)
- Emergency journal-failure log (normally absent):
  `~/.local/log/safe-sync/safe-sync-emergency-YYYY-MM-DD.log`
- Cloud event replicas: `<profile remote base>/.audit/`
- macOS desktop app: `~/Applications/Safe Sync.app`

Use `safe-sync config show` to inspect the effective configuration without
manually editing the JSON file.

## 15. Uninstall

Run the uninstaller from the cloned repository:

```bash
./uninstall.sh
```

Normal uninstall removes the app, service, command, runtime, state, and logs,
but preserves `~/.safe-sync` and its Dropbox authorization.

To also remove local Safe Sync configuration and authorization, use the
explicitly confirmed purge:

```bash
./uninstall.sh --purge
```

Neither form deletes Dropbox backups or Safe Sync's remote trash.

## 16. Command Summary

- `safe-sync help` prints this complete guide.
- `safe-sync setup` creates or validates setup and folders.
- `safe-sync connect-dropbox` connects or reconnects Dropbox.
- `safe-sync status` reports service, watcher, queue, and sync health.
- `safe-sync backup` queues a backup; its options support direct dry runs.
- `safe-sync start`, `stop`, and `restart` control the backend.
- `safe-sync logs` queries, filters, exports, configures, and replicates the
  structured audit journal.
- `safe-sync doctor` runs configuration and remote health checks.
- `safe-sync folders` manages watched folders.
- `safe-sync profiles` manages local computer identities.
- `safe-sync computers` lists computers published to Dropbox.
- `safe-sync list` browses a remote path.
- `safe-sync compare` performs a read-only remote/local comparison.
- `safe-sync receive` creates a staged receive or clone job.
- `safe-sync pull` is a compatibility alias for staged receive.
- `safe-sync jobs` reviews, applies, reconciles, and rolls back receive jobs.
- `safe-sync links` manages granular linked-folder declarations and status.
- `safe-sync history` lists retained versions and stages selected recovery.
- `safe-sync config show` displays effective local settings.
- `safe-sync autostart backend` controls login startup.
- `safe-sync rclone` runs the Safe Sync-managed rclone when advanced recovery
  or authorization requires it.
