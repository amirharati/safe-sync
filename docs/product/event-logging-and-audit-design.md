# Event Logging and Audit Design

## Status

Proposed on 2026-08-09 as a required implementation and review gate before
broad real-world dogfooding. No product code implements this design yet.

This document is the authoritative design for Safe Sync event recording,
diagnostic verbosity, bounded local retention, per-profile cloud replication,
and audit inspection. It deliberately does not make the bounded event journal
a recovery database. A later recovery feature may consume selected events but
must write them to a separate durable store with its own retention rules.

## Why This Feature Comes Before Dogfooding

Dogfooding is useful only if Safe Sync can later answer, in order:

- Was the native watcher running, degraded, or replaced by polling?
- Which local scope was observed to change, and when?
- Was a backup queued, delayed, started, interrupted, or retried?
- Which paths were added, modified, removed, skipped, or failed?
- Was the changed-path generation published locally and remotely?
- What did a receive, merge, checkpoint, rollback, or reconciliation job do?
- Did cloud log replication succeed, lag, or lose events because the bounded
  local journal wrapped while offline?

Today these answers are split among a mutable status file, daily text logs,
rclone output, backup reports, generations, and receive-job journals. The new
design creates one structured event model and one recording path for
observability so those surfaces cannot tell unrelated stories.

## Decisions at a Glance

| Area | Decision |
| --- | --- |
| Source of truth | One ordered structured event stream per profile and installation is authoritative for historical observability. |
| Transaction state | Generation records and receive-job journals remain authoritative for backup and apply state; the event journal does not replace them. |
| Event format | Versioned newline-delimited JSON with stable event names, IDs, correlation IDs, and typed fields. |
| Levels | Audit events are always recorded; a configurable diagnostic level controls additional error, warning, normal, debug, and trace detail. |
| Capacity | Each local profile stream is a fixed-size segmented circular ring with an atomically persisted cursor. |
| Wrap behavior | Seal the active segment, advance the cursor, and atomically replace the oldest slot. Never overwrite bytes inside an active event. |
| Cloud | Sealed segments replicate automatically to the remote base owned by the event's profile. |
| Cloud safety | Upload a new segment before publishing a manifest that references it; remove superseded cloud segments only afterward. |
| Offline behavior | Logging and sync continue within the bound. If an unreplicated slot must be overwritten, record an explicit sequence gap and surface degraded audit health. |
| Privacy | Never record credentials or file contents. Paths are relative to a configured folder and path detail is configurable. |
| Recovery | A future allowlisted durable-event sink uses separate files and storage. Circular debug/audit segments are never recovery authority. |

## Product Boundaries

### What the event journal is

- A chronological record of what Safe Sync observed, decided, attempted, and
  completed.
- The source for the control panel Activity view, CLI log queries, exported
  support bundles, and cloud-side dogfood analysis.
- A bounded audit trail intended for debugging and operational review.

### What it is not

- A copy of backed-up file contents.
- A replacement for remote trash or Dropbox revisions.
- A replacement for immutable backup generations.
- A replacement for receive-job/checkpoint state needed to resume or roll back
  an interrupted apply.
- A permanent compliance archive.
- A promise of full point-in-time restoration.

If the mutable status cache disagrees with the event journal about historical
activity, the event journal wins. If the event journal disagrees with a job
journal about whether a transactional action is safe to resume, the job
journal wins. If it disagrees with a published backup generation about the
contents of that successful generation, the generation wins. Events must be
emitted from the same commit points to make disagreement exceptional and
detectable.

## One Logical Event Pipeline

All backend components use one `EventRecorder` library. It owns schema
validation, redaction, sequence allocation, segmentation, cursor updates, and
notifications to readers.

```text
watcher / scheduler / backup / generation / receive / CLI API
                              |
                              v
                    schema + redaction
                              |
                              v
                 profile EventRecorder + lock
                              |
                 +------------+-------------+
                 |                          |
                 v                          v
        bounded local segment ring   current-status projection
                 |
                 v
        background cloud replicator
                 |
                 v
       profile-owned remote audit ring
```

The control panel does not maintain an independent audit log. UI actions call
the backend, which records accepted/rejected requests and outcomes. UI-only
rendering faults may use a separate local frontend diagnostic file, but they
must not claim that a sync action occurred.

When the daemon is stopped, direct CLI commands use the same recorder and a
process-safe lock. Sequence allocation and append happen while holding that
lock. The lock is held only for the small local write, never during rclone or
cloud I/O.

## Stream Identity and Ordering

There is one logical stream for each tuple:

```text
profile_id + machine_id + install_id + stream_epoch
```

- `profile_id` chooses the correct configuration and cloud destination.
- `machine_id` is the human-stable owner identity.
- `install_id` prevents a replacement installation from silently continuing
  another installation's sequence.
- `stream_epoch` changes only when the journal is initialized or a cursor is
  irrecoverably rebuilt.
- `sequence` is a monotonically increasing unsigned integer within an epoch.
- `event_id` is deterministically composed from install, epoch, and sequence.

Wall-clock timestamps are stored in UTC for cross-computer comparison. A
process monotonic timestamp is also stored where elapsed-time analysis matters.
Sequence, not wall-clock time, defines order within one stream. Ordering across
computers is not invented; correlation uses generation/job IDs and timestamps.

Events produced before setup can identify a profile are written to a small
local-only `system` stream. They are not silently uploaded to whichever
profile becomes active later.

## Event Envelope

Every line is one complete JSON object:

```json
{
  "schema_version": 1,
  "event_id": "evt_<install>_<epoch>_000000000184",
  "sequence": 184,
  "occurred_at": "2026-08-09T18:42:11.418Z",
  "recorded_at": "2026-08-09T18:42:11.421Z",
  "stream": {
    "profile_id": "dogfood",
    "machine_id": "macbook",
    "install_id": "...",
    "epoch": "..."
  },
  "run_id": "run_...",
  "component": "backup",
  "channel": "audit",
  "severity": "info",
  "event_type": "backup.path_result",
  "correlation": {
    "operation_id": "op_...",
    "folder_id": "projects",
    "generation_id": null,
    "job_id": null
  },
  "durability_hint": "audit_only",
  "data": {
    "path": "my-cool-app/src/main.py",
    "result": "modified",
    "bytes": 4821
  }
}
```

Required envelope fields never move into an untyped message string. Each
`event_type` has a versioned payload definition and required/optional fields.
Human-readable messages are derived for UI and text export, not treated as the
canonical representation.

Unknown event types and newer optional fields remain readable. A reader must
reject a newer incompatible schema version visibly rather than guess.

## Audit Events and Diagnostic Levels

`channel` and `severity` are separate concepts.

### Audit channel

Audit events describe user-visible or safety-relevant state transitions. They
are recorded at every configured diagnostic level, including `quiet`:

- Watcher started/stopped/degraded and polling fallback entered/exited.
- A debounced folder change was accepted, including the affected folder and
  coalesced relative paths or counts.
- Backup queued, delayed, started, completed, failed, or interrupted.
- Per-path backup result: added, modified, removed, moved to trash, skipped,
  or failed.
- Generation publication started/completed/failed.
- Configuration or profile changed, with secrets and unsafe values removed.
- Receive/clone/link/history job staged, verified, approved, applied,
  reconciled, rolled back, blocked, or failed.
- Log segment loss, cursor rebuild, corruption, or cloud-replication gap.

Audit does not mean permanent. These events remain subject to the bounded ring
until a future durable sink explicitly allowlists them.

### Diagnostic channel and configured level

The configured level controls additional implementation detail:

| Setting | Records |
| --- | --- |
| `quiet` | Audit events plus diagnostic errors only. |
| `normal` | Audit events plus errors, warnings, lifecycle summaries, and useful rclone summaries. This is the production default. |
| `debug` | Normal plus raw watcher/coalescing decisions, queue reasoning, rclone debug lines, hashes used for comparison, and cloud-replication decisions. |
| `trace` | Debug plus high-frequency internal state transitions intended for short, supervised investigations. |

Changing the level records `logging.level_changed` with old/new values and the
actor (`ui`, `cli`, or config reload). It takes effect without restarting the
daemon. Invalid values leave the previous level active and create a visible
configuration error.

Dry runs use the same event names with `effect: "none"`; they cannot be
mistaken for applied work.

## Initial Event Catalog

Names use `<domain>.<past-tense-or-state>` and remain stable after release.

### Runtime and watcher

- `runtime.started`, `runtime.stopping`, `runtime.stopped`
- `watcher.started`, `watcher.degraded`, `watcher.recovered`
- `watcher.change_detected`, `watcher.change_coalesced`
- `reconciliation.started`, `reconciliation.completed`,
  `reconciliation.failed`

Raw filesystem notifications exist only at `debug`/`trace`. Normal audit uses
the debounced/coalesced change accepted by the scheduler, preventing an editor
save from flooding the journal while still proving why a backup was queued.

### Backup and remote publication

- `backup.queued`, `backup.delayed`, `backup.started`
- `backup.path_result`
- `backup.completed`, `backup.failed`, `backup.interrupted`
- `generation.publication_started`, `generation.published`,
  `generation.publication_failed`
- `registry.published`, `registry.publication_failed`

`backup.path_result` is generated from rclone's machine-readable combined
report, not by parsing localized human log text. A final summary contains
counts by result and the rclone exit code. A successful generation records the
same `operation_id`; publication occurs only after the backup result is known.

### Receive, merge, and recovery-facing work

- `job.created`, `job.stage_started`, `job.staged`, `job.stage_failed`
- `job.policy_changed`, `job.apply_started`, `job.action_completed`
- `job.applied`, `job.blocked`, `job.reconciliation_required`
- `job.rollback_started`, `job.rolled_back`, `job.rollback_blocked`
- `history.version_staged`
- `link.change_detected`, `link.baseline_accepted`

The durable job journal is updated first when it is the transaction authority;
the event is then emitted with the journal revision and job ID.

### Logging health

- `logging.level_changed`, `logging.path_policy_changed`
- `logging.cursor_rebuilt`
- `logging.corruption_detected`, `logging.events_dropped`
- `logging.cloud_degraded`, `logging.cloud_recovered`

Successful upload of every segment is tracked in the replication cursor but
does not create another ordinary event for every upload. Otherwise recording
upload success would generate another segment to upload forever. Only state
transitions and periodic aggregated summaries become events.

## Privacy and Redaction

The recorder applies redaction before sequence allocation and disk write.

Never record:

- Dropbox/rclone tokens, authorization JSON, passwords, cookies, or headers.
- File contents or snippets.
- Environment-variable values not explicitly allowlisted.
- Complete command lines that may contain credentials.
- Absolute local paths in normal operation.

Paths are scoped to `folder_id` and stored as normalized relative paths. A
separate setting controls path detail:

| `path_detail` | Behavior |
| --- | --- |
| `relative` | Store safe folder-relative paths. Default for dogfood because file-level diagnosis is required. |
| `hashed` | Store a stable per-install keyed hash and extension/category only. |
| `none` | Store counts without path identity. |

Redaction failure rejects the unsafe payload and records a safe
`logging.event_rejected` summary. It must never fall back to writing the raw
object.

## Bounded Circular Storage

### Why segmented rather than in-place byte overwrite

The required behavior is a circular buffer: once the configured maximum is
reached, the writer returns to the beginning and replaces the oldest data.
Overwriting arbitrary bytes inside one JSONL file can leave half an event,
requires fragile recovery after a crash, and is difficult to replicate safely
to Dropbox.

Safe Sync therefore implements the same logical ring using fixed segment
slots:

```text
~/.local/state/safe-sync/event-journal/
  <profile_id>/
    cursor.json
    active.tmp
    slots/
      0000.jsonl
      0001.jsonl
      ...
```

Initial defaults per profile:

```json
{
  "level": "normal",
  "path_detail": "relative",
  "max_local_bytes": 67108864,
  "segment_bytes": 1048576,
  "cloud_enabled": true,
  "max_cloud_bytes": 67108864,
  "cloud_flush_interval_seconds": 60
}
```

This creates 64 one-MiB logical slots and a 64-MiB bound per profile, plus one
temporary active segment and small manifests. Configuration validation requires
at least four segments and enforces safe upper bounds. `max_*_bytes` is a byte
budget, not a promised number of days; debug and trace consume it faster.

### Write and wrap protocol

1. Acquire the profile journal lock.
2. Validate and redact the event, allocate the next sequence, and serialize
   one complete JSON line.
3. If that line would exceed `segment_bytes` and the active segment is not
   empty, seal the existing active segment with start/end sequence, event
   count, byte count, and SHA-256.
4. Atomically replace the current slot with the sealed segment and atomically
   update `cursor.json` to reference it and advance the slot cursor modulo the
   configured slot count.
5. Append the new line to the current `active.tmp`, flush it, and atomically
   persist the next-sequence cursor. Startup reconciliation treats a complete
   active line as authoritative if a crash occurs between those two writes.
6. Release the lock.

An individual oversized event is stored with a truncated safe payload and a
`payload_truncated` marker; one event is never split across slots.

At wrap, the next slot is the oldest. Replacing it is the defined overwrite-
from-beginning behavior. Readers use cursor sequence ranges, not filename
order. They ignore temporary files and validate hashes before returning a
sealed segment.

A flush timer seals a non-empty partial segment for cloud replication even
when it has not reached `segment_bytes`; daemon shutdown does the same. Routine
seal/upload success is represented in cursor/manifest state rather than by a
new event, preventing the logging subsystem from generating an endless stream
of events about its own successful work.

Audit lines are flushed before the operation continues. Terminal safety events
(`backup.completed`, `backup.failed`, generation publication outcomes, and job
apply/rollback outcomes) plus segment and cursor transitions are also synced to
stable local storage. Debug/trace-only lines may be batch-flushed. This policy
must be measured during implementation and may be optimized only without
weakening the interruption tests.

### Crash and corruption behavior

- Atomic replacement protects the last valid slot and cursor.
- Startup scans the active file to the final complete newline and truncates
  only an incomplete tail.
- If `cursor.json` is missing/corrupt, rebuild it from valid segment headers
  and hashes, start a new stream epoch only if ordering cannot be recovered,
  and record that fact.
- Corrupt segments are quarantined, never silently skipped. Queries show the
  exact missing sequence range.
- Disk-full or permission errors mark audit health degraded in the independent
  status projection. They do not modify user files or falsely report a backup
  as successful.

## Automatic Per-Profile Cloud Replication

Every event is assigned to a profile before recording. The background
replicator uses that profile's configured rclone remote and `remote_base`; it
never uploads a segment to the currently active profile merely because the
active profile changed later.

Remote layout:

```text
<profile.remote_base>/.audit/
  <profile_id>/
    <machine_id>/
      <install_id>/
        manifest.json
        segments/
          <epoch>-<start>-<end>-<sha256>.jsonl
```

Only the owning installation writes this tree. Other computers may inspect it
through authenticated Safe Sync discovery but never append to or repair it.

### Publication protocol

1. Select sealed local segments after the replication cursor.
2. Upload each segment using its immutable sequence/hash name.
3. Read back metadata or content hash sufficient to verify publication.
4. Upload a new remote manifest to a temporary unique name.
5. Atomically publish/replace `manifest.json` so it references only verified
   segments.
6. Advance the local replication cursor.
7. Delete cloud segments no longer referenced by the bounded manifest only
   after the new manifest is visible.

`max_cloud_bytes` bounds the segments referenced by the current manifest.
Publication may temporarily use one additional segment plus a temporary
manifest while switching generations; cleanup then returns it to the bound.

This is replication, not a two-writer synchronization protocol. The local
ordered event stream creates events; the cloud holds a verified replica of
sealed portions. If the local installation is lost, the cloud copy can be
exported for analysis, but a replacement install starts a new `install_id` and
stream rather than appending to the old one.

Cloud upload runs outside watched folders and never triggers a backup. It uses
the existing serialized remote-operation lane or an explicitly rate-limited
metadata lane so audit replication cannot compete aggressively with file
backup. Backup traffic has priority.

### Offline and bounded-spool behavior

Cloud failure never blocks file backup. Sealed segments remain pending locally
until connectivity returns. Because the journal is intentionally bounded, an
extended outage can reach the oldest unreplicated slot.

When that happens Safe Sync:

- Does not exceed the configured disk bound.
- Overwrites the oldest slot as required.
- Persists the exact dropped sequence range and count in the cursor.
- Shows `Audit degraded: events not replicated before local wrap` in status,
  UI, and CLI.
- Includes the gap in the next cloud manifest and emits
  `logging.events_dropped` when recording becomes possible.

No query may present the remaining events as a complete interval across a
known gap.

## Configuration and User Surfaces

The canonical configuration lives in the Safe Sync profile/config model. UI
and CLI edit the same validated fields; they do not maintain separate
preferences.

Proposed CLI:

```bash
safe-sync logs status
safe-sync logs show --since 2h
safe-sync logs show --event backup.path_result --folder projects
safe-sync logs level normal
safe-sync logs level debug --for 2h
safe-sync logs export --since 24h --output safe-sync-audit.jsonl
safe-sync logs cloud-status
```

`--for` temporarily raises verbosity and automatically returns to the previous
level, preventing accidental long-running trace output.

The control panel Activity view provides:

- Current logging level and a time-limited Debug toggle.
- Local/cloud usage, capacity, oldest/newest event, last verified upload, and
  known gaps.
- Filters for profile, folder, component, event type, severity, operation,
  generation, and job.
- A chronological operation view that groups watcher detection, backup, file
  results, generation publication, and final outcome by correlation ID.
- Export and Open Local Log Folder actions.

The existing status file remains a replaceable projection for fast tray reads.
The daily free-form text log becomes a compatibility export during migration
and is removed only after all required rclone/error information is represented
by structured events. There must not be two independently written production
logs after migration.

## Future Durable Events for Recovery

The event envelope includes `durability_hint`, initially always
`audit_only`. This reserves schema vocabulary; it does not make an event
durable today.

A later design may introduce a small allowlist such as:

- Verified backup generation published.
- Remote version/trash object created and verified.
- Receive replacement checkpoint created and verified.
- Recovery version expired or deliberately deleted.

Those events must be written synchronously from their authoritative commit
points into a separate immutable durable store, for example:

```text
<profile.remote_base>/.durable-records/<profile>/<machine>/<install>/...
```

That store will have an explicit retention/reconstruction design and must not
share circular files, cursors, cleanup, or capacity settings with the audit
journal. Only allowlisted events go there. Debug events, watcher noise, raw
rclone lines, and UI diagnostics never do.

## Failure Cases That Must Be Designed In

- Process crash during event append, segment seal, slot replacement, or cursor
  update.
- Daemon and direct CLI command emitting concurrently.
- Invalid/corrupt cursor, truncated active tail, corrupt sealed slot, or hash
  mismatch.
- Disk full, read-only state directory, or permissions changed.
- One event larger than a segment.
- Clock moves backward or machines have significantly different clocks.
- Profile switch while segments are pending upload.
- Two profiles using different remote bases or credentials.
- Dropbox offline, unauthorized, rate-limited, full, or interrupted mid-upload.
- Local ring wrapping before cloud publication.
- Remote manifest updated while stale segments await cleanup.
- Debug/trace floods caused by noisy filesystem events.
- Secrets or absolute paths appearing in exceptions or rclone output.
- Log replication generating its own endless stream of log events.
- Upgrade from the legacy daily text log and rollback to an older app build.

## Implementation Phases

### Phase 0: Schema and fixtures

- Define the versioned envelope, event catalog, payload validators, redaction
  rules, and golden JSONL fixtures.
- Define configuration bounds and migration from the existing `log_dir`.

### Phase 1: Local recorder and circular ring

- Implement locked sequence allocation, active-tail recovery, segment sealing,
  atomic cursor updates, ring wrap, querying, corruption/gap reporting, and
  deterministic tests.
- Keep existing logging active until equivalence is proven.

### Phase 2: Core instrumentation

- Instrument runtime, watcher, scheduler, rclone backup report, generation,
  registry, receive/job, reconciliation, rollback, and configuration events.
- Correlate every operation across its complete lifecycle.
- Convert status and human text into projections of structured data where
  practical.

### Phase 3: Cloud replication

- Implement per-profile immutable segment publication, verified manifest
  update, bounded remote cleanup, offline backlog, rate limiting, and gap
  reporting.
- Prevent profile misrouting and remote multi-writer behavior.

### Phase 4: CLI, UI, and canonical help

- Add structured queries, timed debug mode, cloud status, Activity UI, and
  export.
- Generate repository, UI Help, and headless CLI documentation from the same
  canonical guide sections.

### Phase 5: Migration and dogfood gate

- Run structured and legacy logging together only during a bounded comparison
  period.
- Prove every required dogfood question is answerable from structured events.
- Remove the independent daily text writer after equivalence and update tests,
  installer, updater, uninstaller, and support bundle behavior.

Future durable recovery events are a separate reviewed phase, not part of this
implementation.

## Required Verification

Automated tests must cover:

- Exact schema, redaction, diagnostic filtering, and audit-always-on behavior.
- Concurrent writers with unique contiguous sequences.
- Segment boundary, multiple wraps, ordering, size bound, and oversized event.
- Crash injection before/after each file/cursor atomic replacement.
- Active-tail repair, cursor rebuild, corrupt-slot quarantine, and visible
  sequence gaps.
- Normal/debug/trace volume and timed level rollback.
- Profile switching and two profiles with distinct remote bases.
- Cloud success, offline backlog, auth/rate-limit failure, interrupted upload,
  verified retry, manifest publication, and bounded cleanup.
- Wrap before replication with exact dropped-range accounting.
- No credentials, file contents, unsafe exception text, or absolute local paths
  in fixtures and generated support exports.
- No self-sustaining events produced by successful log replication.

Disposable macOS and Linux dogfood acceptance requires:

1. Create, modify, rename, and delete known files.
2. Prove the event chain from watcher detection through successful generation.
3. Cause one backup failure and prove its reason/outcome is distinct from
   success.
4. Restart and hard-interrupt disposable processes, then prove event-tail
   repair and visible incomplete operations.
5. Run enough debug activity to wrap the local ring and verify oldest-first
   replacement with intact later events.
6. Disconnect cloud replication, create backlog, reconnect, and verify ordered
   publication; separately force a bounded-wrap gap and verify it is explicit.
7. Switch profiles and prove every cloud segment reached only its owning
   profile's remote base.
8. Inspect the same correlated operation through CLI, control panel, local
   export, and cloud export.

Broad or important folders should not enter dogfood until these checks pass or
an explicit review narrows the gate.

## Deferred by This Design

- Permanent/compliance-grade audit retention.
- Full point-in-time folder reconstruction.
- Dropbox revision API integration.
- Recovery decisions driven from a circular journal.
- Cross-install merging into one ordered stream.
- Remote log search service or external telemetry provider.
- Automatic upload of frontend crash dumps or file contents.
