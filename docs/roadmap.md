# Roadmap

## Current Delivery Roadmap

Installation and setup are the next product milestone. The authoritative
contract is in [installation-and-setup-plan.md](operations/installation-and-setup-plan.md):
source install first, desktop and headless modes, managed rclone, explicit
Dropbox setup, safe update/uninstall, then two-machine real-world testing
before release packages or Windows support.

The first dogfood implementation of the major cross-computer feature is
complete and specified in
[Linked Folders, Safe Transfer, and Recovery Design](product/linked-folder-transfer-design.md).
It covers granular linked subfolders, automatic read-only change detection,
three-way comparison, staged/checkpointed user-approved merge, clone/import,
and recoverable history. The design document records the shipped review scope
and explicitly deferred revision-provider/retention/low-latency polish.

Structured observability is now the required gate before broad dogfooding and
is specified in
[Event Logging and Audit Design](product/event-logging-and-audit-design.md).
It defines a single event model, configurable diagnostic detail, a bounded
segmented circular journal, automatic per-profile cloud replication, and a
separate future path for allowlisted durable recovery events.

## Open Issues

### Immediate next-fix order

1. Preserve the completed 2026-08-14 run before changing or reinstalling
   anything. Its fallback reconciliation completed all five folders, emptied
   the durable queue, and published the recovered 14,964-change folder-5
   generation; retain its logs, reports, generation, and final status as
   acceptance evidence.
2. In one focused maintenance pass, implement `RETRY-001` and `LOG-002`,
   including clearing stale failed-folder/progress fields as soon as recovery
   starts. This source pass is implemented with automated retry/interruption
   coverage; commit and deploy the reviewed result before the clean rerun.
3. Reset preparation completed on 2026-08-14: the final audit/reports/generations
   are preserved, the tray and backend are stopped, only the disposable
   `amirs-macbook-pro` payload/manifests/audit/registry targets were removed,
   and the prior local state/logs were recoverably archived. The Ubuntu registry
   and separate `test/` namespace remain. Reinstall the reviewed commit once.
4. Repeat one final clean Stage 1 run. Include a controlled transient remote
   read failure plus the SIGTERM/SIGKILL boundary cases and verify prompt
   bounded retry, exact-child cleanup, recovered generation publication,
   complete audit evidence, and final local/remote convergence.
5. Keep `PERF-001` experimental during that rerun. Do not advance to Stage 2
   recovery or Stage 3 multi-machine work until `GEN-002`, `LOG-001`,
   `RETRY-001`, and the Stage 1 acceptance evidence pass. Later product issues
   such as remote purge, profile import, notifications, and Dock polish are not
   blockers for this rerun.

### GEN-001: Retry generation publication without aborting the backup set

**Priority:** critical; fix before resuming Stage 1 dogfooding or trusting a
multi-folder backup.

**Status:** implemented and installed on 2026-08-12; automated regression
coverage passes. The clean one-profile dogfood rerun is active and has already
completed its first folder while preserving the remaining four in the durable
queue during a real Dropbox throttle.

**Observed during the clean one-profile restart on 2026-08-10:** the `dist`
payload sync exited successfully with zero remaining changes. Safe Sync then
attempted to publish its immutable generation record, but Dropbox's
`upload_session/append_v2` call timed out waiting for HTTP/2 response headers.
Rclone returned exit code 5. Safe Sync converted that post-backup metadata
failure into a fatal folder-set result and stopped at folder 1 of 5.

A later run exposed the payload-side form of the same reliability gap: a
temporary Dropbox directory-list timeout returned rclone exit 5 after five
files had already copied. Safe Sync discarded the partial attempt context,
skipped the remaining folders, and did not queue an immediate retry.

The daemon did not immediately retry the pending generation, did not continue
with the other configured folders, and did not queue another backup. It
returned to `watching` while health remained `generation publication failed`.
At inspection time the clean remote contained `dist`, `test_sync`, and a
partial `tools`; `temp` and `workbench_agent2-history-safe` were absent.

This is not evidence of local corruption: the local source is untouched and
the `dist` payload reached Dropbox. It is nevertheless a backup-completeness
failure. A transient metadata request can strand later folders and can leave a
successfully updated remote payload without the generation evidence required
for linked-folder detection and an auditable generation chain.

Required behavior:

- Persist a pending generation publication after payload sync succeeds and
  retry it independently with bounded adaptive backoff.
- Make publication idempotent across timeout ambiguity and daemon restart;
  retry the same generation safely without breaking its parent chain.
- Do not prevent unrelated configured folders from receiving their backup
  attempt solely because one folder's post-backup metadata publication failed.
- Report payload state and metadata state separately: for example,
  `payload complete; generation pending`, rather than implying either full
  success or an active file-transfer failure.
- Clear the active error only after the pending generation is verified remote;
  retain each failure and retry in the structured journal.
- Cover immutable-record and `latest.json` failures, timeout-after-remote-write
  ambiguity, repeated throttling, process interruption, restart reconciliation,
  and continuation through the remaining folders.

Implemented behavior:

- rclone request-level retries are restored while whole-operation retries stay
  at one so combined change reports remain unambiguous.
- A profile-scoped atomic queue retains the precise payload or generation stage,
  attempt count, and net successful path changes for every unfinished folder.
- Temporary exit 5/timeouts use bounded adaptive backoff. Unrelated later
  folders continue unless Dropbox reports a provider-wide throttle, and a retry
  processes only unfinished folders.
- Generation bodies and IDs are persisted before upload. Immutable and latest
  objects are read back after every write, including an apparent timeout, so an
  ambiguous successful upload is accepted and a restart reuses the same ID.
- No-change cycles no longer publish empty generations. Runtime status exposes
  configured, completed, and pending folder lists and clears stale warnings when
  work resumes.
- Regression tests cover partial-copy exit 5 and continuation, restart-safe
  generation retry, timeout-after-write verification, no-op suppression, net
  change accumulation, and scheduler backoff preservation.

### GEN-002: Reconcile a completed payload report after daemon interruption

**Priority:** critical; fix before Stage 2 recovery or any linked-folder test
that depends on complete generation history.

**Status:** implemented on 2026-08-13; automated interruption-boundary
regression coverage passes. Each daemon attempt now persists its operation and
combined-report identity before rclone starts, atomically commits the report
hash/counts/net changes and successful generation stage before audit expansion,
and recovers an unfinished report before the next convergence retry. Rclone
runs in a tracked process group; graceful shutdown terminates that exact group,
and startup verifies/stops a recorded orphan before starting new work. The
clean real-provider interruption matrix remains required before closure.

**Observed during the overnight Stage 1 run on 2026-08-12:** the initial
`workbench_agent2-history-safe` rclone process completed at 21:36:03 after a
2h27m provider-throttled upload. Its persisted combined report contains 14,964
added paths and zero report errors. One minute later launchctl explicitly
unloaded/reloaded the daemon while Safe Sync was still processing the large
per-path report, before it atomically advanced the durable queue to generation
publication. The cause of the two external unload/load actions is not yet
identified; macOS evidence shows this was not an rclone crash.

After restart, Safe Sync compared the folder again, correctly found all 14,964
paths already present remotely, reported zero changes, removed the queue item,
and skipped generation publication. Six later whole-profile reconciliations
also found zero changes, so the remote payload is converged, but there is no
local `latest.json` or retained `generation.published` event for the folder's
initial upload. The on-disk combined report is sufficient evidence that the
change plan existed, but current restart logic does not associate or reconcile
it.

Required behavior:

- Persist operation/report identity in the queue before starting rclone and
  atomically retain the parsed net change set before expensive per-path event
  emission.
- On startup, reconcile an unfinished payload-stage item with its report and a
  fresh remote comparison. If the payload is already converged, publish the
  recovered generation instead of treating it as a new no-change cycle.
- Make per-path audit emission resumable/bounded so thousands of events cannot
  hold the only payload-to-generation transition open for minutes.
- Prevent a new worker from overlapping an orphan child, and explicitly stop or
  adopt the exact child on graceful restart. Cover SIGTERM/SIGKILL and the
  payload-complete/report-processing crash windows in the interruption matrix.
- Surface `payload converged; generation recovery pending` until the recovered
  generation is verified rather than marking the entire folder fully complete.

### RETRY-001: Promptly recover ambiguous transient rclone failures

**Priority:** high; fix before the next clean Stage 1 acceptance rerun.

**Status:** implemented in source on 2026-08-14; not yet installed. The exact
Dropbox error is retained as a temporary remote failure, first retry waits 30
seconds, repeated attempts use bounded exponential delays, and an expired retry
backoff bypasses the unrelated 120-second normal backup interval. Durable
reports/net changes survive repeated attempts and daemon restart. Status exposes
the retry attempt/countdown and clears obsolete failure, interruption, backoff,
file, and progress projection fields at retry start and final success. The UI
renders the live retry countdown ahead of the prior attempt's progress text.
Regression coverage reproduces the exit-1 error, repeated 30/60-second attempts
across a new daemon API state, eventual one-time accumulated generation
publication, and immediate scheduler execution when retry backoff expires.

**Observed on 2026-08-14:** after macOS wake/restart recovery, folder 5 resumed
with 2,110 planned transfers. At 10:52:35 Dropbox returned one destination-
directory read error for
`data/experiments/enrich-fetch/a1-2026-05-30T22-fetch-400/bodies/265-developers.google.com`:
`unexpected error occurred`. Rclone retained `Errors: 1 (retrying may help)`,
continued copying, recorded 2,442 added report paths with zero individual
failed-transfer paths, and exited 1 at 11:31:23.

A direct read-only listing of that exact remote directory later returned exit
0, proving the provider read error was transient. Safe Sync's temporary-error
classifier recognizes exit 5 and specific timeout/network strings but not this
generic Dropbox response, so it treated the attempt as fatal. The daemon stayed
alive and the schema-2 queue/report plus recovered 14,964-change set remained
safe, but no retry countdown was shown and no immediate retry was scheduled.
The normal 1,800-second fallback finally started a new reconciliation at
12:01:24 and restored health to `ok`.

That reconciliation exited 0, committed a zero-new-change comparison, and used
the durable accumulated changes to publish
`gen_20260814T160238Z_67cb88254a` with all 14,964 changes. The cycle completed
all five folders and emptied the pending queue at 12:03:04. Runtime health is
correctly `ok`, but the idle status projection still retains the obsolete
`failed_folder` value, reinforcing the status-cleanup requirement below.

This was therefore neither data corruption nor a permanently dead daemon, but
it was a real 30-minute backup-completeness delay presented as an indefinite
error. A longer configured fallback would make the delay longer.

Required behavior:

- Treat a known Dropbox destination-list/read failure as retryable. For other
  ambiguous non-authentication, non-configuration rclone exits with a durable
  pending folder, prefer bounded convergence retry over a terminal idle state.
- Preserve the committed attempt report and accumulated net changes across
  retries; a successful no-change convergence must still publish the recovered
  generation exactly once.
- Enter an explicit `retry_pending` state with the reason, attempt number, and
  countdown. Use bounded adaptive backoff, avoid a rapid retry loop, and retain
  attention state when repeated attempts continue to fail.
- Clear active `last_error`, `failed_folder`, old transfer totals, current file,
  and other failed-attempt projection fields when retry scanning begins, while
  retaining the historical failure in the audit journal.
- Add regression coverage for a generic exit-1 directory-read failure after
  successful copies, daemon restart during the delay, repeated persistent
  failure, fallback recovery, final generation publication, and accurate UI
  state throughout.

### LOG-001: Protect audit history from diagnostic floods

**Priority:** fix before another long Debug/Trace dogfood run.

**Status:** first containment pass implemented and installed on 2026-08-12.
Debug now keeps rclone at INFO while retaining Safe Sync's
own detailed events. Raw rclone DEBUG is reserved for Trace, and every rclone
operation caps repetitive raw lines at 2,000 while retaining errors, warnings,
provider failures, phase changes, and aggregate stats, followed by an explicit
suppression summary. During the initial resumed `tools` soak, journal sequence,
size, and gap count remained unchanged while per-file INFO progress continued
in the UI. A later hardening pass must still give audit events
physically protected capacity independent of diagnostics and add the full
budget-exhaustion retention test below.

**Repair implemented on 2026-08-13:** unreplicated diagnostics now stop before
the final quarter of journal slots, reserving that physical capacity for audit
events. Backup results are recorded as a hashed report summary plus bounded
250-path/256-KiB batches instead of one full event per path. The daemon attempts
verified replication between completed folders. Cloud publication is serialized
with a process-safe lock and now writes a verified content-addressed immutable
manifest followed by a verified latest pointer, eliminating the temporary-file
`moveto` race. Automated capacity, batching, and publication tests pass; repeat
the long offline/throttled real-provider test before closing the issue.

**Overnight result on 2026-08-13:** the cap worked for `tools` (2,744 repetitive
lines suppressed), but the 16,592 retained `backup.path_result` events plus
other diagnostics still drove the 64 MiB journal from 18 to 32 permanent gaps
before 01:47. All 63 pending segments eventually replicated and current cloud
backlog is zero, but the local/remote audit history remains incomplete. Cloud
manifest publication also degraded three times: one Dropbox
`too_many_write_operations`, one source/destination shape error, and one
temporary-manifest `from_lookup/not_found`; all three later emitted
`logging.cloud_recovered` and current replication health is good. Audit events
need protected capacity, large-report aggregation, replication opportunities
between folders, and race-safe idempotent cloud-manifest publication.

**Observed during one-profile dogfood on 2026-08-09:** a large repository with
many `.git/objects` produced hundreds of thousands of rclone Debug lines during
one multi-hour backup. Cloud journal replication correctly stayed out of the
active backup lane, but the shared 64 MiB local ring filled and overwrote
unreplicated segments. Gap reporting worked, but important audit history can
be displaced by high-volume diagnostics, which is the wrong retention
priority.

Give always-on audit events protected capacity independent of noisy diagnostic
events. Bound, sample, or aggregate raw rclone/watcher diagnostics per
operation; replicate sealed segments between folders; and keep accurate live
cloud-backlog/gap status during long operations. Add a test that exceeds the
diagnostic budget while a remote operation remains active and proves lifecycle,
failure, and per-path audit evidence is retained even when diagnostic detail is
dropped.

**Fresh confirmation on 2026-08-12:** timed Debug launched the active rclone
child at DEBUG and, in roughly ten minutes of the `tools` run, filled the 64 MiB
journal, created 14 new gaps, and emitted repeated `logging.events_dropped`
warnings while all 63 segments remained pending behind the active backup.
When timed Debug expired the effective journal level returned to Normal, but
the already-running child continued writing DEBUG output because its command
line cannot change in place. The fix must cap/sample rclone diagnostics per
operation and ensure a temporary diagnostic level cannot continue producing an
unbounded raw child stream after expiry.

### LOG-002: Classify graceful backup interruption without a false error

**Priority:** minor; fix in the next maintenance round after the active Stage 1
run completes. Do not interrupt the current backup to deploy it.

**Status:** implemented in source on 2026-08-14; not yet installed. SIGTERM and
SIGINT now carry their signal number through a dedicated `DaemonShutdown` path.
An in-flight payload records warning-level `backup.interrupted` with operation,
folder, report identity/existence, and recovery-pending state, then re-raises for
the normal exact-child shutdown path; it no longer falls through to error-level
`backup.failed`. Regression coverage proves the report remains `running` in the
durable queue, is recovered on the next attempt, publishes its accumulated
change exactly once after convergence, and clears interruption/failure state.

**Observed on 2026-08-14:** macOS fully woke at 10:51 after an overnight idle-
sleep cycle and restarted the GUI launchd session. Launchd sent SIGTERM to the
previous daemon. Safe Sync correctly signaled and waited for its exact rclone
child, emitted `runtime.stopping` and `runtime.stopped`, restarted without an
orphan, recovered the prior 14,964-change report, and resumed with only 2,110
file transfers remaining. Runtime health correctly returned to `ok` with no
current warning or error.

The audit stream nevertheless records an error-severity `backup.failed` event
with `reason: DaemonShutdown` and `error: signal 15`. This makes an expected,
successfully recovered lifecycle transition look like a real backup failure.

Required behavior:

- Handle `DaemonShutdown` separately from unexpected backup exceptions.
- Record a correlated interruption/cancellation lifecycle event at info or
  warning severity, including the signal, operation, folder, report identity,
  and whether restart recovery is pending.
- Preserve the later `backup.report_recovered` evidence and link it to the
  interrupted operation; do not erase or rewrite history.
- Keep genuine rclone exits, unhandled exceptions, failed graceful termination,
  and unrecoverable reports as error-level backup failures.
- Add SIGTERM regression coverage proving clean child exit, no orphan overlap,
  no false error event, durable report recovery, and eventual generation
  publication.

### STATUS-001: Clear expired backoff warnings after retry resumes

**Priority:** fix after the current Stage 1 dogfood review and before Stage 2
recovery dogfooding; do not interrupt the active backup to deploy it.

**Status:** implemented and installed on 2026-08-12. The new clean run reports
an actual active Dropbox backoff after folder 1 without retaining any prior-run
warning. Live verification passed the corresponding transition: when retry
began, health returned to `ok`, `last_warning` became null, and folder 2 entered
the scan phase with cleared progress fields.

**Observed during one-profile dogfood on 2026-08-09:** the Status view showed
`Dropbox rate limited ... cooling down for 300s` while the daemon was actively
copying folder 5 of 5. The stored backoff had expired hours earlier and its
remaining time was zero, but `last_warning`, `failed_folder`, and backoff
metadata survived into the successful retry cycle.

When a retry begins or demonstrably resumes work, clear active warning/failure
projection fields while retaining the historical warning in the event journal.
The UI must never simultaneously claim an active cooldown and show a live
sync unless a new throttle actually applies. Cover backoff entry, expiry,
retry start, active progress, success, and repeated throttling.

The completed status-health pass also separates current audit health from
historical completeness. Permanent circular-journal gaps remain visible as
`Historical gaps` in Activity but no longer force the app headline to Warning.
Activity provides a `Recent Warnings` view for the last 24 hours, while stale
warning text cannot override an actively healthy sync/transfer/publication.

### UI-001: Distinguish configured folders from current sync folder

**Priority:** fix after the current basic one-profile logging dogfood review,
when the owner returns from travel.

**Status:** implemented and installed on 2026-08-12. Live status reports all
five configured folders separately from the active folder position; final
visual review remains with the owner.

The Status view's former singular `Folder` row displayed only the daemon's
current or most recently processed folder. While idle it can therefore show
one folder even when the active profile has several enabled folders, making a
healthy configuration look incomplete.

The clean-restart observation had five configured folders while Status showed
only `Folder: tools`, confirming the ambiguity.

The fix shows `Configured folders` as an explicit count and complete name list,
and relabels the runtime value
as `Current folder` while work is active and `Last folder` while idle. During
multi-folder backup, retain the useful `name (index/total)` progress. Keep the
quick panel and full control panel consistent, and add UI regression coverage
for idle, active, and stopped backend states. This issue is presentation-only;
the confirmed active configuration contains all five enabled folders.

### PROGRESS-001: Show stable backup completion progress

**Priority:** fix before restarting the clean Stage 1 dogfood baseline.

**Status:** implemented and installed on 2026-08-12; automated regression
coverage passes. Clean live verification observed the explicit comparison
completion marker, then a fixed 13/13 transfer and finalization for folder 1.
Folder 2 subsequently entered scanning with no percentage or inherited totals,
then finalized at a fixed 3/3.

The former Status projection showed whichever raw rclone line arrived last.
Its discovered-so-far totals changed during traversal, a bare `Transferring:`
line could replace useful detail, and the result could not answer how much
work remained.

Backup now uses rclone's check-first mode so comparison finishes before
uploads begin. Status deliberately shows `Scanning and comparing` without a
fabricated percentage during discovery, then shows a stable file percentage,
file and byte totals, and rclone's approximate ETA during transfer, followed
by `Finalizing folder backup` and `Backup cycle complete`. This rearranges the
existing comparison rather than adding a second pre-scan. The tradeoff is that
the first upload starts later and rclone retains the transfer backlog in
memory; verify that behavior with the current disposable five-folder profile
before considering larger production trees.

**Live edge case:** the clean `tools` retry began with a complete comparison
set of 5,767 files, but Dropbox rejected three batch commits with
`too_many_write_operations` and rclone subsequently reported 5,764 as the
denominator. Preserve the comparison-phase total independently from rclone's
mutable post-error stats, and show failed/retry-pending files separately so a
provider error cannot make total work appear to shrink.

**Installed update on 2026-08-12:** implemented a stateful progress tracker that
freezes the comparison-phase file and byte plan, recomputes percentages against
that plan, and counts unique failed paths separately. Regression coverage
replays the observed 5,767-to-5,764 denominator shrink. The resumed `tools` run
completed comparison at 2,026 remaining files / 1.083 GiB and kept that plan
fixed through the initial transfer sample with zero failed files.

### PROGRESS-002: Show exact whole-profile backup-cycle progress

**Priority:** implement in the next UI/status pass after the current clean
Stage 1 run; do not interrupt the active backup to deploy it.

**Status:** implemented and installed on 2026-08-12. Status now labels the
per-file row `Current folder progress` and
adds an `Overall backup` row derived from the durable pending queue, displaying
completed, active, and waiting folder counts. Successful cycle completion also
persists an empty pending set and the complete configured-folder ID set. The
preserved three-item queue rendered as `2/5 complete · 1 active · 2 waiting`
after restart while `tools (3/5)` remained the named current folder.

The current stable file percentage is intentionally scoped to the active
folder, but the Status view does not say that clearly enough. Add a separate
whole-profile summary such as `2/5 folders complete · 1 active · 2 waiting`,
derived from the durable queue so it remains accurate across cooldowns,
temporary failures, and daemon restarts. Relabel the existing percentage as
`Current folder progress` and keep the current folder name/position visible.

Do not manufacture a combined file percentage by weighting folders equally or
by using totals discovered so far. A reliable all-folder file/byte percentage
would require comparing every pending folder before transfers begin (or
persisting an equivalent complete plan), which adds startup delay, memory, and
Dropbox API work. Treat that as a separate design decision only if the exact
folder-completion summary proves insufficient during dogfooding.

### PERF-001: Make Dropbox small-file backups practical

**Priority:** critical and first to implement before the next clean Stage 1
run. Continue observing the active run, but do not interrupt it merely to deploy
an unmeasured flag change.

**Status:** experimental direct-mirror tuning implemented and installed on
2026-08-12 and revised for the next clean run on 2026-08-13. Backup commands
now explicitly use Dropbox's integrity-checked synchronous batch mode with
batch size 32, a five-second batch dwell, and a conservative 8 transfers
(reduced to 4 on a retry). Async batching remains prohibited. The literal
`too_many_write_operations` response is classified directly as a
provider cooldown, and regression coverage verifies the command and retry
classification. The first comparison will resume the existing partial remote
to isolate throughput; clean-fixture hash verification remains required before
closing this issue.

Initial resumed measurement is promising but not yet the clean acceptance
result. The old run sampled about 1.2 files/s. After reinstall, the same partial
`tools` remote reached 518/2,026 remaining files in 3m30s including comparison;
the latest one-minute interval completed 284 files (about 4.7 files/s) at
2.41 MiB/s with no failed files, warning, or error. Continue the run and retain
the later clean-fixture/hash gate before choosing these values as production
defaults.

The largest-folder result shows that the current values are not yet acceptable
as production defaults. `workbench_agent2-history-safe` needed roughly 2h27m
for a 14,964-path / 338.893 MiB initial report and repeatedly received Dropbox
300-second `too_many_requests` delays, although it ultimately converged with no
report errors. Six later full-profile reconciliations reported zero changes for
all five folders, proving correctness, but some no-change comparisons were also
slow under provider throttling. Keep the direct-mirror experiment open and
compare more conservative transfer/batch values in the isolated benchmark.

The clean `tools` run demonstrates a pathological but realistic developer-tree
case. The exact filter policy admits 10,082 files / 2.367 GB, including 6,410
files at or below 4 KiB and 2,037 more at or below 64 KiB. Nearly all content is
the Flutter SDK: `flutter/packages` (3,568 files), `flutter/dev` (2,909),
`flutter/bin` (1,998 files / 2.295 GB), and `flutter/examples` (1,528). A live
45-second window advanced about 57 files but only 242 KiB (roughly 1.2 files/s
and 5-7 KiB/s).

Dropbox is the demonstrated bottleneck rather than local CPU: rclone repeatedly
receives `too_many_write_operations`, its pacer rises as high as two seconds,
and batch commits have failed individual files that the durable queue must
retry. Rclone's Dropbox documentation recommends batch uploads and specifically
notes larger batches/transfers for many small files, but Safe Sync currently
uses rclone's defaults. Before changing production defaults, run an isolated
real-Dropbox A/B fixture with representative tiny files comparing current
settings against explicit synchronous batch size/transfer concurrency, measure
files/s, bytes/s, throttles, retry correctness, memory, and final hashes, then
choose conservative provider-specific settings.

Safe Sync's provider classifier must also recognize Dropbox's literal
`too_many_write_operations` / `too many write operations` response as a rate
limit. The current Debug child additionally emits `pacer: Rate limited`, which
makes this run enter the correct durable cooldown path by coincidence. At
Normal rclone verbosity the literal error may be the only evidence and would
currently fall through to a fatal generic rclone exit instead of a queued
provider retry. Add direct classification and regression coverage independent
of diagnostic verbosity.

Also separate backup policy from transport tuning. Regenerable SDK caches such
as `flutter/bin/cache` may be appropriate user-selected exclusions, but Safe
Sync must not silently exclude an arbitrary tracked tree. Offer documented
ignore presets or a preflight warning for cache/vendor-heavy roots instead.

#### Small-file architecture direction

Direct mirroring remains the first implementation path because it is
transparent in Dropbox, independently inspectable with ordinary tools, and the
simplest physical layout for selective transfer between computers. First test
the strongest safe version of that model: synchronous verified batching,
measured concurrency, durable provider-aware retry, and bounded diagnostics.

Do not couple the logical file model to that physical layout. Compare,
selective sync, clone, and recovery should consume a verified manifest through
a storage interface rather than assume every logical path is always a loose
Dropbox object. This preserves a migration path to an immutable packed-small-
file store if tuned mirroring misses the acceptance target.

A future packed store would group small files into immutable hash-named packs
and publish a manifest mapping logical path, content hash, size, metadata, pack
ID, offset, and length. Upload and hash-verify packs before atomically publishing
the generation; use tombstones for deletion and garbage-collect only objects no
retained manifest references. Selective sync would still operate on logical
paths and extract only the needed pack data. This can be integrity-safe, but it
adds substantial implementation and test scope: consistent pack creation while
files change, range/fallback reads, interrupted publication, history retention,
compaction, garbage collection, and independent disaster recovery.

If both fast protection and a human-browsable Dropbox mirror are ultimately
required, consider a two-state projection rather than making safety wait for
thousands of loose writes: publish verified packs/manifests first (`Protected`),
then materialize the ordinary file mirror in a lower-priority queue
(`Dropbox mirror catching up`). This duplicates some storage and cannot remove
the eventual Dropbox write cost, so it is a later optional design—not the first
fix. The UI and logs must never confuse pack protection with mirror completion.

Decision gate: retain direct mirror as the initial default, build the storage
boundary now, and introduce packed or dual-layer storage only if the isolated
tiny-file benchmark proves verified synchronous batching cannot meet the agreed
throughput/reliability target.

### REMOTE-001: Optional remote backup purge

**Priority:** after real two-machine install testing.

Provide an explicit, deliberately-confirmed way to remove Safe Sync's Dropbox
copy of a selected profile or folder without touching any local files. This
must be available from both CLI and control panel, show the exact remote paths
to be removed (including associated remote trash/registry data), require an
unambiguous confirmation, and never be part of ordinary uninstall or local
purge. The UI may offer this alongside local cleanup, but they must remain
separate choices.

### PROFILE-001: Import a remote profile onto a new computer

**Priority:** after real two-machine install testing.

Allow a user to intentionally import a registered remote profile when moving a
workspace to a new computer. Import must be an explicit ownership-transfer
operation: inspect the remote profile and its folders first, select new local
paths, create a new local identity by default, and never activate an imported
profile automatically. Document that one profile must not be actively used by
two machines at the same time. Later work may detect concurrent ownership, but
must not rely on that detection for safety.

The linked-folder design's **Clone to this computer** flow covers the safer
default of creating a new local identity. Reusing/transferring the original
profile identity remains a separate advanced operation under this issue.

### HEADLESS-001: Optional remote failure notifications

**Priority:** after real two-machine install testing.

Headless installations have no tray icon. Current diagnosis is through
`safe-sync status`, `safe-sync logs`, the systemd user-service journal over
SSH, and the Linux headless install's interactive-Bash health hint. Add opt-in
notification destinations such as email or a generic webhook
for persistent error/reconnect-required states. Notifications must never expose
Dropbox tokens, local file contents, or full transfer paths by default.

### MAC-001: Installed tray app still appears in the Dock/taskbar

**Priority:** polish; defer until after real two-machine installation testing.

**Observed:** the production macOS app installed at `~/Applications/Safe Sync.app`
stays out of the Dock while its tray icon is idle, but appears in the
Dock/taskbar whenever a user clicks the tray icon and the quick popup becomes
visible. It disappears again when that popup closes. The earlier development
launch did not show this behavior. This is specifically the quick popup, not
an always-visible app-launch entry.

**What has already been tried:** the bundled `Info.plist` contains
`LSUIElement=true`, and the Tauri startup sets
`ActivationPolicy::Accessory`. Both are present in the built production bundle.

**Investigation:** the quick popup is currently an ordinary borderless
`NSWindow`, which may promote the accessory app while visible. Compare an
AppKit non-activating `NSPanel` and an `NSPopover` implementation that keeps
the popup open across its internal actions. Avoid changing the successful
first-click positioning behavior; preserve a usable normal control panel.

## Phase 0: Docs and Existing Setup

- Capture workflow decisions.
- Define safe defaults.
- Keep current rclone remotes and filters available for reference.
- Do not build a tray UI yet.

## Phase 1: CLI Wrapper

Build a small `safe-sync` command that can:

- Show config.
- Run backup for this machine.
- Run backup in dry-run mode.
- Pull a file/folder from another machine backup.
- List known machines.
- Print status and latest log path.

Commands:

```bash
safe-sync backup
safe-sync backup --dry-run
safe-sync pull <machine> <remote-path> <local-path>
safe-sync list <machine> <path>
safe-sync status
```

Initial implementation must target `~/safe-sync-test` only. Do not run against a broad or important work folder until the test folder workflow is proven.

## Phase 2: Watch Daemon Skeleton

Add a daemon structure and render install files, but do not auto-install it yet.

- Add watcher state machine docs.
- Add config fields for debounce/cooldown/fallback/backoff.
- Add placeholder modules for watch orchestration.
- Keep `~/safe-sync-test` as the only target.
- Review design before full implementation.

## Phase 3: Watch Daemon Implementation

Implement watcher-first backup triggering:

- Watch local folder for changes.
- Debounce noisy changes.
- Coalesce changes while a backup is running.
- Respect Dropbox rate-limit backoff.
- Run fallback timer periodically.
- Support dry-run daemon testing. Basic polling daemon is implemented first; native filesystem events can be added later.

## Phase 4: Scheduler

Add install helpers:

- Repo-level `./install.sh` command for service install and `safe-sync start` / `safe-sync stop` commands for daemon control.
- Linux service/autostart controls and Windows Task Scheduler later if needed.

The scheduler runs the watch daemon, not raw backup, after the watch daemon is reviewed.

## Phase 5: Status File and Logs

Write machine-readable status:

```json
{
  "state": "idle",
  "last_start": null,
  "last_success": null,
  "last_error": null,
  "last_command": null
}
```

Keep human logs in a stable path.

## Phase 6: Tray Status UI

Build a thin Tauri tray/menu app:

- Read `safe-sync status` or status JSON.
- Show ok/syncing/stopped/stale/error.
- Menu actions for start daemon, stop daemon, backup now, open logs, refresh, and quit tray.
- Keep sync logic in the existing CLI/daemon only.
- Split autostart into backend daemon autostart and tray UI autostart.
- Add backend autostart CLI commands before the tray depends on them. macOS is first; Linux/Windows remain backlog.

Work through documented checkpoints in `docs/operations/tauri-tray-workflow.md`.

## Phase 7: Control Panel UI

Add a second-level Tauri window for day-to-day configuration and selective transfer:

- View and update safe numeric settings.
- View configured local folders and add another folder.
- View known computers from the remote registry.
- Build a selective pull form using `safe-sync list` and `safe-sync pull`.
- Support local simulation with alternate remote paths/profiles for testing, without running multiple daemon watchers.

Backlog guardrail before broad automation: add a daemon process lock so there is exactly one daemon watcher process, in addition to the existing one-backup-at-a-time lock.

## Phase 8: Safe Receive and Granular Linked Folders (implemented for dogfood review)

The initial direct-pull workflow is replaced by the phased design in
[Linked Folders, Safe Transfer, and Recovery Design](product/linked-folder-transfer-design.md):

- Publish per-folder backup generation records.
- Add structured scoped two-way and three-way comparison.
- Stage and verify incoming content on the destination filesystem.
- Apply through durable checkpoints and an interruption-safe journal.
- Present remote data as Computer -> Folder -> Contents.
- Add safe whole-folder clone/import.
- Link an entire folder or a granular subfolder such as
  `Projects/my-cool-app` to a peer scope.
- Detect changes automatically but require Review & Sync before every merge.
- Add selected-version recovery before claiming full point-in-time restore.

Late packaging/polish backlog:

- Package the Python backend as a real app-owned executable so macOS permission prompts show `Safe Sync` instead of `Python`.
- Keep this as an end-of-project packaging step, after the core daemon/UI workflow is stable.

## Not Planned Initially

- Custom sync engine.
- Making the daemon depend on the tray UI.
- Complex conflict browser.
- Automatic multi-way live sync for all projects.
- Editing rclone internals.


Windows service support remains TODO.
