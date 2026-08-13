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

Safe Sync deliberately has no whole-transfer deadline. Large models and data files may take as long as they need while progress continues. Rclone still has short connection and inactive-network timeouts. A temporary exit 5 or timeout is retained in the profile's durable per-folder queue and retried with bounded backoff; completed folders are not repeated, and unrelated later folders continue unless Dropbox imposes a provider-wide cooldown.

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
