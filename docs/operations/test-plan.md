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

## Staged Dogfood Progression

### Dropbox-native recovery acceptance

The accepted `RECOVERY-001` design keeps no Safe Sync-owned trash or duplicate
snapshot payload. Dropbox's native version/deleted-file history is the recovery
payload. Safe Sync records one full revision-metadata baseline followed by
compact deltas so complete historical folders can be reconstructed in excluded
local staging. Existing trash is not deleted during implementation or migration.

Before resuming recovery dogfooding, use only disposable files to prove that a
normal backup without `--backup-dir` preserves recoverable Dropbox history for
an overwrite and deletion, and record how Dropbox presents a local rename.
First confirm that the post-update reconciliation publishes a complete baseline
even when no payload changed. Then choose a later cycle, stage its complete
folder, and verify unchanged, modified, deleted, renamed, and later-added paths
against the manifest before opening the staging folder. Confirm the watched
folder is untouched. For advanced selected-file recovery, download a chosen Dropbox
revision into excluded per-job local staging without changing the live remote,
compare it with the current local path, and exercise both `Keep both` and
explicit `Replace local`. Resume backup and prove replacement creates a new
forward Dropbox version. Separately test broad Dropbox Rewind, which restores
only to the existing remote location; stage/reconcile that state back to local
before resuming. Prove that the daemon cannot silently overwrite a remote-only
restoration while recovery is paused.

Older trash-path checks below document tests of the currently installed
implementation and remain applicable only until `RECOVERY-001` is deployed.

Do not combine all workflows in one test period. Complete and review each
stage's structured logs before moving to the next stage.

### Stage 1: Basic one-profile watch and backup (current)

Use only the current computer/profile. Enable temporary Debug logging for two
hours and exercise ordinary disposable local activity:

- Create, edit, rename, and delete files inside watched folders.
- Create, rename, and remove nested directories, including empty directories.
- Optionally add or remove a disposable watched-folder configuration; verify
  that removing it stops management without deleting local or remote data.
- Allow automatic watcher-triggered backups and optionally use Backup Now.
- Do not restore, receive remote data, transfer, compare, link, clone, merge,
  import, or adopt another profile during this stage.

At the end of the window, review the Activity journal and verify watcher
detection, debouncing, queued work, per-path results, generation publication,
remote-trash handling, cloud log replication, and final local/remote state.
Do not advance until unexplained missing, duplicate, or contradictory events
have been resolved.

**Targeted incremental acceptance, 2026-08-15:** reinstall the reviewed build
without deleting the established disposable remote baseline. Let the startup
full-profile reconciliation finish and wait for `Watching`. Then create a new
uniquely named subfolder and a few small files under the disposable `temp`
folder. The watcher should queue `temp` only after the configured debounce,
Status should identify a targeted `1/1 changed folders` cycle, and no unrelated
folder comparison should run. If a file changes while another operation is
already active, it may wait for that operation to finish, but it must become the
next targeted cycle without waiting for the fallback timer. Review the event
chain from `watcher.change_detected` through a targeted `backup.queued`, payload
result, generation publication, audit replication, and healthy completion.
After that passes, separately exercise modify, delete, rename, nested-directory,
and empty-directory behavior.

**Incremental mutation sequence:** use the established
`~/temp/incremental-test-2026-08-15` baseline and allow each targeted cycle to
return to `Watching` before starting the next action. First append unique
content to `modify.txt`; then rename `rename-old.txt` to `rename-new.txt`; then
delete `delete.txt`. Keep `unchanged.txt` untouched as a control. Verify the
first generation reports one modification and retains the prior remote version
in timestamped trash; the second reports the rename as one addition plus one
removal and retains the removed remote name; the third reports one removal and
retains the deleted remote file. Every cycle must target only `temp`, leave the
local tree untouched except for the owner's action, publish exactly once, and
end healthy. Directory-only activity remains a known acceptance gap tracked as
`ACTIVITY-001` and should be repeated after that fix.

**Overnight result, 2026-08-13:** payload behavior passed on the disposable
five-folder profile. Status is healthy and watching with `5/5` complete, an
empty durable queue, zero failed files, and no current warning/error. The large
initial upload converged, and six later complete profile reconciliations found
zero changes in every folder. Cloud log replication eventually recovered and
drained its backlog to zero.

Stage 1 is not yet fully closed. An explicit daemon service reload occurred
after the largest rclone payload completed but before Safe Sync published its
14,964-path generation, so restart reconciliation later skipped that generation
as `no_changes`. The bounded journal also accumulated 32 permanent gaps before
cloud replication caught up, and cloud-manifest publication transiently failed
three times before recovering. Track these as `GEN-002` and `LOG-001`; fix and
repeat the targeted interruption/audit acceptance checks before Stage 2.

The repair build was implemented on 2026-08-13 and passes 126 backend tests.
The clean rerun must prove that an interruption after payload completion still
publishes the recovered generation, no stale/orphan rclone overlaps the next
worker, diagnostic pressure preserves the audit reserve, between-folder cloud
flushes finish with a verified content-addressed manifest pointer, and the
8-transfer baseline/4-transfer retry materially reduces Dropbox throttling.

**Clean-rerun incident, 2026-08-14:** folders 1-4 completed and folder 5 safely
resumed after an overnight macOS sleep/restart. Dropbox then returned one
generic `unexpected error occurred` while rclone read a destination directory.
Rclone continued its transfers but exited 1 with one retained error. Safe Sync
preserved the durable report and pending generation state but missed the
temporary-error classifier, remained idle/error for 30 minutes, and recovered
only when the normal fallback reconciliation ran. The same remote directory was
readable afterward. Track this as `RETRY-001`. Do not erase this run yet: first
preserve its evidence. The fallback comparison subsequently exited 0, published
generation `gen_20260814T160238Z_67cb88254a` containing the accumulated 14,964
changes, completed all five folders, and emptied the durable queue. Audit health
remained good with no gaps. Status returned to healthy/complete but retained a
stale `failed_folder` value, which is also part of `RETRY-001`.

Before the next clean reset, implement prompt bounded recovery for this case
and the `LOG-002` graceful-shutdown event correction in one reviewed build.
Then preserve the current evidence, clean only the disposable machine-owned
remote namespace, reinstall once, and repeat Stage 1 with controlled transient
failure and interruption cases. Do not advance to Stage 2 until that run passes.

**Source repair, 2026-08-14:** `RETRY-001` and `LOG-002` are implemented but not
installed. Automated coverage reproduces the exact exit-1 directory-read error,
30/60-second bounded retries across a simulated restart, immediate scheduling
when retry backoff expires, retained net changes and one-time final publication,
stale status cleanup, and graceful SIGTERM report recovery without a false
`backup.failed`. The complete backend suite passes 130 tests and the production
UI build passes. Real-provider clean-rerun acceptance is still required.

**Clean reset prepared, 2026-08-14:** the final audit, reports, and generations
were preserved before stopping the tray/backend. Exact remote checks now prove
the disposable `amirs-macbook-pro` payload, manifests, audit, trash, and registry
targets absent; the Ubuntu registry record and separate `computer-backups/test`
namespace remain. Prior local runtime state and raw logs were recoverably moved
to `safe-sync-stage1-final-archive-2026-08-14-1219` sibling directories, and the
active state/log directories are empty with mode 700. Configuration and the
Safe Sync-owned rclone authorization remain in place. The next action is one
`./install.sh --update`, followed by fresh status/journal verification.

### Stage 2: Recovery on one profile/computer

Remain on the same computer/profile. Exercise selected-version history and the
safe receive job used for recovery: stage, inspect, apply with explicit policy,
verify checkpoints, and perform a conditional rollback. Do not introduce a
peer machine, second profile, linked folder, cross-computer transfer, or merge.
Review recovery/job logs and prove that live local files are never replaced
before approval and that rollback never overwrites a later edit.

### Stage 3: Cross-computer and advanced workflows

Only after Stages 1 and 2 pass, introduce a second disposable machine/profile
as needed. Exercise selective receive, exact local/remote diff, conflict
policies, linked-folder three-way merge, clone/import/export semantics, and
granular subfolder synchronization. Review every staged/apply/rollback path
and the correlated logs before expanding beyond disposable data.

The interruption matrix below remains a separate destructive test using its
own isolated fixture and remote prefix.

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
.git/HEAD
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
- Excludes a `.git/` at the selected root, plus nested `node_modules/`,
  `.venv/`, and `dist/` directories.
- Does not touch broad or important work folders.

Real test backup should only happen after dry-run output is reviewed.

## Transfer Duration And Dropbox Backoff

During the first real test on 2026-07-12, Dropbox returned:

```text
too_many_requests
Trying again in 300 seconds
```

Safe Sync deliberately has no whole-transfer deadline. Large models and data files may take as long as they need while progress continues. Rclone still has short connection and inactive-network timeouts. A temporary exit 5 or recognized timeout is retained in the profile's durable per-folder queue and retried with bounded backoff; completed folders are not repeated, and unrelated later folders continue unless Dropbox imposes a provider-wide cooldown. `RETRY-001` extends that prompt bounded recovery to ambiguous Dropbox directory-read exit-1 failures that currently wait for the normal fallback reconciliation.

During testing, confirm that Status names the pending folder and retry delay,
then let the daemon retry it. Do not manually hammer Dropbox in a loop.

## Interrupted-Sync Simulation

This experiment determines actual shutdown behavior instead of assuming that
rclone makes every form of process termination safe. Run it only against a
fresh temporary source tree, isolated Safe Sync config/runtime paths, and a
unique remote below `dropbox:computer-backups/test/interrupt-*`. Never reuse a
real backup folder.

### Fixture

1. Create a baseline containing small files, a large file that keeps an upload
   active long enough to interrupt, and nested empty directories.
2. Complete and verify one baseline backup.
3. Change one existing file, delete another, add a new file, and replace the
   large file. Record local hashes and remote/trash listings before each run.
4. Capture the exact disposable daemon and child rclone PIDs. Never use a broad
   `pkill` pattern during this experiment.

### Interruption cases

Run every case from a fresh verified baseline:

1. Close and force-quit the tray UI while rclone is transferring. Confirm that
   the independent daemon and rclone child continue and complete.
2. Request a normal backend stop during the large-file upload. Record which
   signals are delivered, how long shutdown takes, and whether the child exits.
3. Restart the backend during the upload, including the same path exercised by
   `./install.sh --update`. Confirm there is never more than one daemon or
   rclone transfer lane.
4. Send a hard kill only to the identified disposable rclone child during a
   new upload and during replacement of an existing remote file.
5. Send a hard kill only to the identified disposable daemon while its rclone
   child is active. Check explicitly for an orphan child before restarting.
6. Interrupt a run that has destination-only files waiting for the default
   delete-after phase.

### Required evidence

- Local file contents and hashes are unchanged in every case.
- No partial upload is exposed under its final remote filename.
- Any already committed files have valid Dropbox hashes; remaining files may
  be old or absent until reconciliation, but must not be silently corrupted.
- An interrupted/error run does not perform pending destination deletions or
  report a successful backup.
- Files moved before an interruption are present either at the live path or in
  Safe Sync's timestamped remote trash.
- No orphan rclone process, duplicate daemon, stale lock, or concurrent retry
  remains after stop/restart.
- A subsequent uninterrupted backup plus remote verification converges the
  live destination exactly to the filtered source.
- Status and logs clearly distinguish interruption, recovery, and final
  success.

Record results separately for macOS launchd and Linux systemd. If graceful
backend stop cannot reliably terminate and reap rclone, implement signal
forwarding with a bounded graceful wait and forced-cleanup fallback before
using important folders.
