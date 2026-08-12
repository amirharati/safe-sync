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

### LOG-001: Protect audit history from diagnostic floods

**Priority:** fix before another long Debug/Trace dogfood run.

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

### STATUS-001: Clear expired backoff warnings after retry resumes

**Priority:** fix after the current Stage 1 dogfood review and before Stage 2
recovery dogfooding; do not interrupt the active backup to deploy it.

**Status:** implemented and installed on 2026-08-12. The new clean run reports
an actual active Dropbox backoff after folder 1 without retaining any prior-run
warning; verify the corresponding clear-on-resume behavior when the retry
begins.

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
