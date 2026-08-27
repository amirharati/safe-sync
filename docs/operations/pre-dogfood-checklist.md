# Pre-Dogfood Checklist

This checklist is the release gate before the next real macOS/Linux dogfood
period. Do not begin the broad-folder test until every required item and its
verification are complete.

## Required fixes

- [x] Replace frequent full-tree polling with native filesystem events on
  macOS and Linux, while retaining an infrequent reconciliation scan.
- [x] Apply the same generated/dependency/cache directory exclusions to watch
  events and reconciliation scans that the rclone filter applies.
- [x] Write configuration and status JSON atomically so an interrupted write
  cannot leave a partial file.
- [x] Make an explicit Backup Now request bypass automatic debounce and
  minimum-interval cooldown while still respecting an active transfer and
  Dropbox backoff.

## Acceptance checks

- [x] Creating, modifying, deleting, and renaming files plus creating/removing
  empty directories produces one debounced backup request.
- [x] Changes under ignored directories do not trigger a backup.
- [x] Idle monitoring does not repeatedly scan a broad `~/projects` tree.
- [x] The periodic reconciliation path still detects a missed change.
- [x] Repeated Backup Now clicks coalesce, and a request received during a
  running operation remains queued for the next safe opportunity.
- [x] Atomic-write failure tests preserve the previous valid JSON file.
- [x] Python tests, frontend build, Rust check, installer shell checks, and a
  broad-tree watcher smoke test pass.

## Verification record

- Backend regression suite: `60 passed`.
- Frontend production build: passed.
- Rust native check: passed.
- Installer/uninstaller shell syntax and diff checks: passed.
- All six managed-watchdog asset URLs and SHA-256 values match the pinned
  PyPI `6.0.0` release metadata.
- Real macOS FSEvents smoke: create, modify, rename, delete, empty-directory
  create/remove, and ignored-output checks passed.
- Disposable Ubuntu 24.04 ARM64 Docker smoke: all `57` tests passed with
  Python 3.12 and the exact checksum-pinned manylinux watchdog wheel. Real
  inotify create, modify, rename, delete, empty-directory create/remove, and
  ignored-output checks passed.
- Synthetic Ubuntu broad-tree smoke: native monitoring started in about
  `0.11s`, remained idle without a snapshot, and used `1,003` inotify watches
  against the container limit of `1,048,576`.
- Broad-tree smoke: native monitoring of `/Users/amir/projects` started in
  about `0.06s` and remained idle without a full snapshot. The old polling
  implementation took `14-19s` for roughly `198,000` included entries.
- Docker does not provide a normal logged-in `systemd --user` session, so the
  Linux service lifecycle remains a deployment check on a real Linux machine;
  it is not part of this watcher correctness gate.

## Dogfood interruption gate

Complete these checks with disposable local data and a unique Dropbox prefix
before testing any folder whose loss would matter. Follow the detailed
protocol in `docs/operations/test-plan.md`.

- [ ] Close and force-quit only the tray during an active upload; prove the
  daemon and its rclone child continue and finish normally.
- [ ] Stop/restart the backend during a large upload; record daemon/rclone
  process behavior and prove no orphan or competing transfer remains.
- [ ] Hard-kill the precisely identified disposable daemon/rclone process at
  upload, overwrite, and delete-after phases.
- [ ] Verify local hashes never change, completed remote files are valid,
  incomplete work is not reported as success, and prior remote versions remain
  recoverable from timestamped trash.
- [ ] Restart after every interruption and prove a normal run converges the
  live remote to the source without duplicate processes or manual repair.
- [ ] Record the observed macOS and Linux behavior and decide whether graceful
  signal propagation must be implemented before expanding dogfood scope.

## Safe receive and linked-folder implementation gate

- [x] Remote/local comparison is read-only and classifies two-way and
  three-way changes without timestamp winners.
- [x] Real pull/receive stages beside the destination and does not modify live
  files before a separate reviewed apply.
- [x] Missing-file add, keep-local, keep-both, replace, delete, and
  leave-staged policies are journaled; replacements/deletions preserve the old
  local path in a checkpoint.
- [x] Destination changes after review block apply, and rollback refuses to
  overwrite edits made after apply.
- [x] Startup reconciliation discovers interrupted jobs and blocks backup while
  a checkpointed-but-unresolved transition remains.
- [x] Clone requires a new/empty destination and commits by same-filesystem
  rename without adopting peer ownership.
- [x] Granular linked folders reject unsafe/overlapping scopes, installation or
  filter mismatches, detect local watcher changes and relevant peer generations,
  and require Review & Sync before staging/apply.
- [x] Machine-wide Recovery Mode durably blocks every outbound backup entry
  point, including the final rclone execution boundary, across restarts.
- [x] Restore opens the exact Dropbox folder, verifies an isolated historical
  export, guides undo-Rewind, and requires two current remote/local equality
  checks before guarded exit.
- [x] Restore, Status, and tray expose guarded Cancel Recovery; equal state
  unlocks without writing, while differing state reconciles local to Dropbox
  and verifies before unlock. Failures remain locked.
- [x] Before a differing-state cancellation, Restore, Status, and tray offer an
  optional exact remote-to-isolated-local safety copy, verify remote stability
  and content hashes, expose Open Saved Copy, and remain locked on failure.
- [ ] Real-provider validation proves Rewind, verified export, undo-Rewind,
  remote stability detection, restart recovery, guarded final unlock, and both
  no-write and reconcile-required cancellation paths.
- [x] `.safe-sync-work` is excluded by a separate mandatory internal rclone
  filter plus watcher/inventory rules, independent of the user's existing
  filter file.
- [x] Backups, Jobs, Linked Folders, Recovery, CLI commands, and the canonical
  repository/UI/headless guide expose the same workflow.

Verification on 2026-08-08: backend `81 passed`, including simulated apply
interruption, later-edit rollback conflict, generation/link detection, and a
real local-rclone selective stage/apply; frontend production build, Rust
native check, CLI help/guide identity, shell syntax, and diff checks passed.
The browser preview surface was unavailable, so visual click-through remains
part of the owner's pre-install review.

## Structured event logging and cloud audit gate

The required behavior is specified in
[`docs/product/event-logging-and-audit-design.md`](../product/event-logging-and-audit-design.md).

- [x] Replace independently written mutable/text histories with one structured
  event pipeline and stable correlation IDs.
- [x] Record always-on audit events while allowing `quiet`, `normal`, `debug`,
  and `trace` diagnostic levels, including a time-limited Debug mode.
- [x] Enforce a crash-safe byte-bounded segmented circular ring with an atomic
  cursor, oldest-first wrap, integrity checks, and visible sequence gaps.
- [x] Replicate verified sealed segments automatically to the owning profile's
  remote base without competing with backups or routing across profiles.
- [x] Expose correlated watcher, backup, per-path, generation, receive, and
  rollback history consistently through CLI and control panel.
- [ ] Pass local wrap/crash/corruption tests plus offline, rate-limit,
  interrupted-upload, profile-routing, redaction, and cloud-gap tests on
  disposable macOS and Linux fixtures.

Implementation-level automated coverage is in place. Keep the final item open
until the destructive/interrupted scenarios have been exercised on disposable
macOS and Linux fixtures against the intended cloud provider.

## Deferred until dogfood evidence

- Direct pull fallback when a daemon socket accepts a connection but returns
  no response. Do not run a competing direct rclone operation without proving
  the daemon no longer owns the work lane.
- Splitting the Tauri Rust module or replacing CLI status subprocesses.
- Replacing rclone error-text classification, Windows support, remote
  notifications, and macOS Dock polish.
