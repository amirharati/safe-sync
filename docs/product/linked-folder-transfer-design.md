# Linked Folders, Safe Transfer, and Recovery Design

## Status

Implemented for the first dogfood review on 2026-08-08. This document remains
the authoritative design for cross-computer comparison, selective transfer,
checkpointed merge, folder cloning, linked folders, and history recovery.

The shipped review scope includes schemas and containment checks, immutable
backup generations, two/three-way comparison, destination-adjacent staging,
explicit checkpointed apply, crash reconciliation, conditional rollback,
safe clone, granular linked folders with automatic local/peer-generation
detection, remote-trash browsing/recovery, headless commands, and matching
control-panel surfaces. Checkpoints are intentionally retained during
dogfood.

Dropbox revision-provider integration, low-latency Dropbox cursors/long poll,
automatic retention cleanup, text-diff/rename suggestions, and claims of full
point-in-time folder reconstruction remain later work. A clone is independent
and may be added as a new watched folder after commit; it is not enabled for
backup automatically.

## Why This Feature Exists

Safe Sync already gives each computer an independently owned Dropbox backup.
It can browse another computer's published folders and queue a selective
`rclone copy`, but the current real pull writes directly into the selected
local destination. Its preview is a shallow listing rather than a true diff,
and it does not create a durable staging job or local recovery checkpoint.

The missing product workflow is a trustworthy way to answer:

- What differs between this local folder and another computer's backup?
- Which files do I want to receive?
- Can I clone the whole folder without creating shared ownership?
- Can two corresponding folders notify me about changes without merging them
  automatically?
- Can I recover a replaced or deleted version later?

The answer is one shared comparison and receive engine, with different UI
entry points for selective transfer, cloning, linked folders, and history.

## Decisions at a Glance

| Area | Decision |
| --- | --- |
| Granularity | Link a complete configured folder or one safe relative subpath such as `Projects/my-cool-app`. |
| Ownership | Every computer continues writing only its own backup and registry records. |
| Detection | Local watcher plus peer backup generations detect possible changes automatically. |
| Synchronization | The user always reviews and approves cross-computer file changes. |
| Comparison | Use a stored common baseline and a three-way scoped diff. |
| Transfer | Download into verified staging on the destination filesystem. |
| Apply | Move staged files into place; never download/copy directly over live files. |
| Conflict | Keep local, keep both, replace selected, delete selected, or leave staged. No timestamp winner. |
| Recovery | Enter machine-wide Recovery Mode, use Dropbox Rewind, export the temporary historical remote into a separate verified local folder, undo-Rewind, and verify current remote/local equality before unlock. |
| Clone | Clone into a new/empty destination and create a new local ownership identity. |
| History | Dropbox owns retained history; Safe Sync keeps only the bounded state of an explicitly initiated recovery transaction. |

## Relationship to the Existing Backup Model

Decision 0001 remains in force:

- Every computer writes only its own remote backup tree and registry document.
- A peer may read that backup but never writes to it.
- Cross-computer changes are applied only to the local computer that approved
  them.
- The local watcher may then back up the accepted result to this computer's own
  remote tree.

Linked folders do **not** create a shared live Dropbox folder. They create a
local relationship between two independently owned folder scopes.

Example:

```text
Mac local scope
  folder: Projects
  subpath: my-cool-app
  path: ~/Projects/my-cool-app

Ubuntu peer scope
  computer: ubuntu-workstation
  folder: work
  subpath: projects/my-cool-app
  remote: dropbox:computer-backups/ubuntu-workstation/work/projects/my-cool-app
```

Only `my-cool-app` is linked. Sibling projects remain unrelated.

## Product Vocabulary

### Computer

A Safe Sync installation with a stable `install_id` and a human-readable
`machine_id`/label. The pair is used to detect replacement installations and
avoid accidental duplicate ownership.

### Backup folder

A configured folder owned by one computer, identified by `folder_id`. The local
absolute path stays local; peers discover its label, ID, and remote path from
the registry.

### Folder scope

Either an entire backup folder or a normalized relative subpath within it.
Scopes enable granular links such as `projects/my-cool-app` without linking the
entire configured `projects` root.

### Linked folder

A local declaration that one local folder scope corresponds to one peer backup
scope. It enables automatic read-only change detection and user-initiated
review; it grants no remote-write authority.

### Baseline

The last folder state the user accepted as common for a link. A three-way diff
compares the baseline with the current local scope and current peer scope.

### Generation

A successful backup publication from a computer. A generation identifies the
parent generation and the paths changed by that backup. It lets peers notice
that a linked scope may have changed without repeatedly scanning the whole
remote tree.

### Receive job

A durable request to compare, stage, verify, and optionally apply remote data
to a local destination. Selective transfer, clone, linked-folder review, and
history recovery all create receive jobs.

### Checkpoint

The prior local files moved aside before approved replacements or deletions.
The checkpoint and job journal support crash recovery and rollback.

## Safety Invariants

These are requirements, not UI preferences:

1. A computer never writes another computer's owned backup tree.
2. Remote change detection never changes local files.
3. Cross-computer apply and deletion are always user-initiated.
4. Remote data is never downloaded directly over a live destination file.
5. Incoming data is staged and verified before it can be applied.
6. A destination is rechecked immediately before apply. A stale comparison
   cannot silently authorize a merge.
7. Missing files may be added by default, but replacement and deletion require
   explicit approval.
8. Every replacement first preserves the prior local file in a checkpoint.
9. An interrupted apply is reconciled from a durable per-file journal before a
   backup of the affected scope may start.
10. Rollback never overwrites work created after the apply. If the installed
    file changed, recovery creates a separate copy for review.
11. Working, staging, checkpoint, and job metadata paths are excluded from
    watcher triggers and backup filters.
12. Dropbox history is a secondary recovery source. Unsynced local content
    must not depend on Dropbox for recovery.
13. Links use stable computer/folder identities and normalized relative paths,
    not display labels or unvalidated raw remote paths.

## Granular Link Model

A proposed local link record:

```json
{
  "schema_version": 1,
  "id": "link_...",
  "label": "My Cool App",
  "local": {
    "profile_id": "...",
    "folder_id": "projects",
    "subpath": "my-cool-app"
  },
  "peer": {
    "machine_id": "ubuntu-workstation",
    "install_id": "...",
    "folder_id": "work",
    "subpath": "projects/my-cool-app"
  },
  "baseline": {
    "accepted_at": "...",
    "local_generation": "...",
    "peer_generation": "...",
    "inventory_path": "..."
  },
  "notifications": true
}
```

The record is local configuration. It does not publish absolute local paths.
A later link invitation/acceptance protocol may exchange stable IDs, but each
computer must choose and approve its own local destination.

### Scope validation

- Normalize separators and remove leading/trailing separators.
- Reject absolute paths, `..`, NULs, and paths escaping the configured root.
- Resolve the local scope and reject symlink traversal outside the configured
  root.
- Treat case-folding and Unicode-normalization collisions as conflicts on
  filesystems where they collide.
- Do not allow overlapping linked scopes on the same local folder in version
  one. A scope may have only one peer link.
- An empty subpath means the whole configured folder.

### Filter compatibility

A peer backup is a filtered view of its local folder. A path missing because a
filter excluded it must never be mistaken for a peer deletion.

- Publish the effective filter-policy fingerprint with registry/generation
  metadata.
- Version one requires matching effective filter fingerprints for both sides
  of an active link. Otherwise the link remains blocked for alignment/review.
- A filter-policy change invalidates the common baseline and requires a fresh
  comparison; it never produces automatic deletion candidates.
- Work/checkpoint exclusions are application-internal and must be applied on
  both sides in addition to the user-visible content filter.

## Remote Publication and Change Detection

### Registry

The existing per-computer registry remains the discovery layer. The control
panel should use friendly labels by default and place raw remote paths under
technical details.

### Generation records

After a successful real backup, the owner publishes a versioned record beneath
its configured remote base:

```text
.manifests/
  <machine_id>/
    <folder_id>/
      latest.json
      generations/
        <generation_id>.json
```

Only the owning machine writes these records. Publish the immutable generation
record first, then update `latest.json`, so a peer never observes a pointer to
an incomplete record.

A generation record should contain:

- Schema version, generation ID, parent generation ID, and completion time.
- `machine_id`, `install_id`, `profile_id`, and `folder_id`.
- Filter-policy fingerprint.
- Changed relative paths and operation type: added, modified, or removed.
- Available after-state size, modification time, and hash/revision metadata.
- Backup result and whether the change report is complete.
- No token, secret, or absolute local path.

Do not persist an entry for every unchanged file in every generation. Capture
changed-path reports from rclone and store periodic inventory checkpoints only
when needed for baseline/history reconstruction. If generations are missing,
the peer must fall back to a full scoped comparison.

### Detection behavior

- The native watcher marks linked scopes with local changes.
- The daemon polls small `latest.json` records with normal debounce and Dropbox
  backoff, initially using a conservative interval.
- A peer change becomes visible only after that peer completes and publishes a
  successful backup generation. Safe Sync must not present this as live access
  to unbacked peer files.
- A peer generation triggers a prompt only if its changed paths intersect the
  linked peer scope.
- A changed peer `install_id` freezes the link as `Peer replaced` until the user
  verifies the new installation and relinks; matching `machine_id` alone is
  not sufficient.
- The app prompts once per stable generation and records snooze/dismiss state.
- A full remote scan happens only when the user opens review, a generation is
  incomplete/missing, or reconciliation requires it.
- Dropbox cursor/long-poll support is a later latency optimization, not a
  prerequisite. The API supports retrieving changes after a cursor and waiting
  for notification without rescanning the account.

## Three-Way Folder Comparison

For every relative path, compare the accepted baseline `B`, current local
state `L`, and current peer state `R`. A missing path is a real state.

| Condition | Classification | Default action |
| --- | --- | --- |
| `L = B`, `R = B` | Unchanged | None |
| `L != B`, `R = B` | Local only | Keep local; peer may detect it after backup |
| `L = B`, `R != B` | Peer only | Select incoming addition/update; deletion remains explicit |
| `L = R`, both differ from `B` | Same change on both | Advance baseline without transfer |
| `L != B`, `R != B`, `L != R` | Conflict | User review required |
| Local missing, peer modified | Delete-versus-modify conflict | User review required |
| Local modified, peer missing | Modify-versus-delete conflict | User review required |

### Fingerprints

- Quick comparison uses normalized path, type, size, modification time, and a
  compatible available hash.
- Content verification is authoritative when metadata cannot establish
  equality. Rclone `check --download` can compare content without modifying
  either side, at the cost of downloading the remote data.
- Client modification time is display information, not a conflict winner.
- A rename may initially appear as an add plus a delete. Matching strong hashes
  may later offer a rename suggestion, but never an automatic conclusion.
- Directory inventories must include empty directories if clone/history claims
  to preserve them.

The UI should group results into Same, Local only, Peer only, Different, and
Errors. Text-file side-by-side content diff is a later presentation feature;
the first engine is a folder-tree and content-equality diff.

## Receive Job Lifecycle

### 1. Plan

Record the stable source identity, remote generation/revisions, selected paths,
intended destination, baseline, comparison results, and requested operation.

### 2. Stage

Create a unique empty staging directory on the destination filesystem. A
recommended layout is:

```text
<destination-parent>/.safe-sync-work/
  staging/<job_id>/
  checkpoints/<job_id>/
  jobs/<job_id>.json
```

If the destination parent cannot host the work directory, choose another
writable location on the same filesystem. If that is impossible, clearly
report that final apply cannot use atomic renames and require a separately
designed cross-filesystem path; do not silently fall back to an unsafe merge.

The app also keeps a small central job index under its state directory so jobs
can be found even when the destination volume is temporarily disconnected.

### 3. Verify

- Verify every complete staged file against captured source size/hash or a
  content check.
- Leave rclone partial files inside staging and never treat them as complete.
- Re-read source generation/revisions. If the peer changed, label the job as a
  staged snapshot and require either a new comparison or explicit approval to
  apply that older captured snapshot.

### 4. Revalidate destination

Immediately before apply, compare every affected destination path with the
fingerprint used during review. Move changed paths back to conflict review.

### 5. Apply

Apply choices are:

- **Add missing:** move staged files that have no destination conflict.
- **Keep local:** do not touch the destination; retain or discard the staged
  copy explicitly.
- **Keep both:** move the incoming file using a deterministic peer and UTC
  timestamp suffix.
- **Replace selected:** move the existing destination into the job checkpoint,
  then move the staged file into place.
- **Delete selected:** move the existing destination into the checkpoint; do
  not permanently delete it.
- **Leave staged:** take no destination action.

All moves on the same filesystem are atomic per path. A clone into a new path
may commit with a single directory rename after verification. A multi-file
merge is not globally atomic, so it relies on the durable journal below.

### 6. Journal and reconcile

For each planned path, atomically persist transitions such as:

```text
planned -> old_checkpointed -> incoming_installed -> verified
```

On interruption, inspect the journal, destination, staging path, checkpoint,
and fingerprints. Finish or roll back only the unambiguous transition; otherwise
mark the path as needing review. The daemon must reconcile an incomplete apply
before it runs an automatic backup for that affected scope.

Journal transitions use atomic file replacement and flush the journal plus its
parent directory where the platform supports it before advancing to the next
filesystem move. Recovery still verifies paths/fingerprints rather than
trusting the last journal write alone.

A receive job waiting for human review must not block unrelated automatic
backups. Only the short apply/reconcile section owns the local mutation lane.

### 7. Complete and retain

- Remove empty staging directories after every selected item is resolved.
- Retain the job manifest and checkpoint during dogfood; do not auto-delete.
- Later retention may be configurable, but cleanup requires a verified apply
  and, for watched destinations, a confirmed subsequent backup/recovery copy.

## Rollback

Rollback reads the job journal in reverse order.

- If the current destination still matches the fingerprint installed by the
  job, move it aside and restore the checkpointed file.
- If the current destination changed after apply, never overwrite it. Restore
  the checkpoint under a clear recovered name or new review job.
- Files added by the job are removed only by moving them into recovery, never
  by permanent deletion.
- Rollback itself is journaled and resumable.

## Selective Transfer

Selective transfer is a receive job whose source selection contains one or
more files/subfolders.

- Browse `Computer -> Backup folder -> Contents`.
- Allow selection at any depth within the published folder.
- Show the exact source breadcrumb and intended local destination.
- Compare only the affected scope when possible.
- Stage and verify selected content.
- Use the normal apply choices; never bypass checkpoints for conflicts.

The current direct real `safe-sync pull` behavior should be deprecated. Its
safe successor should create a receive job. Dry-run remains useful as a
comparison report, not as authorization for a later changed plan.

## Clone / Import

"Clone to this computer" is clearer than separate import/export terminology.
The peer backup already acts as the export.

A clone:

1. Selects an entire peer folder or granular subfolder.
2. Requires a new or empty local destination for the fast safe path.
3. Stages and verifies all content.
4. Commits the complete directory by rename when possible.
5. Creates a new local folder identity owned by this installation.
6. Separately asks whether to add the result as an enabled watched folder.

It never reuses or activates the peer's profile/folder ownership identity. A
non-empty destination is a merge and must enter the normal diff workflow.

## Linked Folders

### Creating a link

1. Choose a local configured folder and optional subpath.
2. Choose a peer computer, backup folder, and optional subpath.
3. Run a full initial comparison.
4. If different, complete or explicitly resolve a receive/merge job.
5. Activate the link only when both scoped inventories are content-equal, then
   capture that common inventory as the baseline.

If the user chooses to keep a one-sided difference, the link stays `Pending
convergence`. The other computer must receive/backup the chosen result before
the common baseline can be established.

Links are local declarations. One computer may create a one-sided link for
change detection and receiving. For the same project to prompt on both
computers, each computer must separately choose/accept its own local scope and
peer scope; neither machine chooses a local path for the other.

### Status

A linked-folder card should show:

```text
My Cool App
This Mac: ~/Projects/my-cool-app
Ubuntu:   work/projects/my-cool-app

4 peer changes | 2 local changes | 1 conflict
[Review & Sync] [Later]
```

Status values include Up to date, Local changes, Peer changes, Changes on both,
Conflict, Peer stale/unavailable, and Review already staged.

### User-initiated sync

`Review & Sync` opens the three-way diff and creates or refreshes a receive job.
Detection and notification are automatic; staging, apply, replacement, and
deletion remain explicit user actions.

### Loop prevention

After a peer change is accepted locally:

1. Store the peer generation and accepted baseline.
2. The local watcher may back up the merged result to this computer's own tree.
3. The peer may observe this computer's new generation.
4. If content already matches, it advances its baseline without prompting or
   transferring the same content again.

Generation ancestry, accepted peer generation, and content equality prevent
prompt loops. A computer cannot remotely mutate an offline peer's local files;
the peer reviews changes when it next runs.

## History and Recovery

### Dropbox-native history

As accepted in `RECOVERY-001`, normal backup keeps no Safe Sync-owned remote
trash or latest snapshot. Dropbox's plan-bounded versions, deleted-file history,
and Rewind are the historical authority. Safe Sync generations and audit are
operational/link evidence only and are not presented as recovery history.

Folder recovery enters a durable machine-wide Recovery Mode, opens the exact
backup folder in Dropbox, copies the temporary rewound state into a separate
verified local export, guides undo-Rewind, and unlocks only after current
Dropbox equals the unchanged watched local folder. Dropbox Rewind remains a
website operation because Dropbox exposes no public Rewind or Rewind-status API.

### Generation history

Generation change records currently support linked-folder notification and
scope filtering only. They are not recovery snapshots and must not be expanded
into a whole-folder reconstruction or time-travel system.

### Dropbox revisions

Dropbox can list file revisions, identify whether a revision is restorable,
and download/restore a particular revision within the account's retention
window. Safe Sync uses those revisions as the primary historical payload for
content that has already reached Dropbox.

The safe default is to download an older Dropbox revision into a receive job.
Calling Dropbox restore directly changes the live remote file and creates a new
revision, so it requires a separate explicit remote-mutation confirmation.
Dropbox cannot recover a local edit that was never uploaded.

## Control Panel Information Architecture

Replace the current raw Computers/Transfer separation with related surfaces:

### Backups

- Computer cards with friendly name, freshness, last successful publication,
  folder count, and ownership (`This computer` or `Remote`).
- Expand a computer to see folder labels and last backup state.
- Folder actions: Browse, Compare with Local, Receive Selected, Clone.
- Raw rclone paths under Technical details.

### Linked Folders

- Local and peer scopes, including granular subpaths.
- Baseline time and peer freshness.
- Change counts and conflict badge.
- Review & Sync, Later, Relink, and Remove Link actions.
- Removing a link changes no files and deletes no backup.

### Jobs

- Comparing, staging, ready for review, applying, interrupted, complete, or
  rolled back.
- Exact planned actions and checkpoint location.
- Resume, Review, Apply Selected, Reveal Staging, Roll Back, and later Cleanup.

### Recovery

- Durable backup pause/resume state and clear local-source warning.
- Dropbox revisions for a selected configured relative path.
- Hash-verified staging plus bounded text diff or binary size/hash summary.
- Keep Current, Keep Both, or explicit Replace through a receive job, never a
  direct remote restore.

The UI must continue showing the exact equivalent CLI command or job operation.

## Proposed CLI Surface

Exact naming may be refined during implementation, but headless workflows need
the same engine:

```text
safe-sync compare <remote-scope> <local-scope>
safe-sync receive create <remote-scope> <local-destination> [--select ...]
safe-sync jobs list
safe-sync jobs show <job-id>
safe-sync jobs apply <job-id> [selection/policy options]
safe-sync jobs rollback <job-id>

safe-sync links add ...
safe-sync links list
safe-sync links status [link-id]
safe-sync links review <link-id>
safe-sync links remove <link-id>

safe-sync recovery status
safe-sync recovery enter <folder-id>
safe-sync recovery mark-rewound
safe-sync recovery export
safe-sync recovery mark-undo-complete
safe-sync recovery verify
safe-sync recovery exit
```

Commands should produce structured JSON internally and concise human output.
The Tauri UI invokes the same backend operations rather than implementing its
own diff or merge logic.

## Daemon and Concurrency Model

- Keep one daemon and one rclone work lane.
- Backups, remote comparisons, staging downloads, and active apply/reconcile
  operations are serialized when they would compete or mutate related state.
- A job awaiting review is persistent data, not an active lane owner.
- Watcher events generated during an apply are coalesced, but backup of the
  affected scope waits until the apply journal reaches a consistent terminal
  state.
- Dropbox rate-limit backoff applies to manifest reads/writes, comparisons, and
  revision requests as well as backup/transfer operations.
- A peer may update its remote source while a job is staged. That is handled by
  source revalidation, not by attempting a distributed lock.

## Failure Cases That Must Be Designed In

- Process termination during download, checkpoint move, install move, rollback,
  or manifest publication.
- Destination volume disconnected while a job exists.
- Out of disk space during staging or checkpoint retention.
- Peer generation changes after comparison or during staging.
- Local destination changes while the review screen is open.
- Missing generation records, broken parent ancestry, or stale registry data.
- Dropbox throttling, expired authorization, and offline operation.
- Case-only names across case-sensitive/case-insensitive filesystems.
- Unicode normalization collisions and platform-reserved filenames.
- Symlinks, broken symlinks, and links escaping a selected scope.
- File-versus-directory conflicts at the same relative path.
- Partial selection where a parent and its child are both selected.
- Nested linked scopes, duplicate links, and replacement installations using a
  previous `machine_id` with a different `install_id`.

Every failure must leave live local data unchanged, checkpointed, or visibly in
an interrupted job. No ambiguous case may be reported as success.

## Implementation Plan

### Phase 0: Fixtures and schemas

- Define versioned schemas for generation records, scoped inventories, links,
  receive jobs, apply journals, and checkpoints.
- Add path normalization, scope containment, collision, and stable-identity
  helpers.
- Build disposable local and rclone-local fixtures for two simulated computers,
  granular subpaths, conflicts, deletes, renames, and empty directories.

**Gate:** schemas round-trip; unsafe paths and overlapping scopes fail before
any file or remote operation.

### Phase 1: Backup generations

- Capture changed-path reports from successful backup runs.
- Publish immutable per-folder generation records and an atomic/latest pointer.
- Cache/read peer generation summaries with backoff.
- Do not change backup ownership or deletion behavior.

**Gate:** two simulated computers publish only their own records; interrupted
publication never exposes a broken latest pointer; linked-scope intersection
detects relevant and ignores unrelated changes.

### Phase 2: Read-only comparison engine

- Produce structured two-way and three-way scoped comparisons.
- Support quick and content-verification modes.
- Record a baseline inventory and classify all change/delete conflict cases.
- Expose comparison through CLI and the backend bridge.

**Gate:** comparison makes no source/destination changes and correctly
classifies same, local-only, peer-only, both-same, conflict, and error cases on
macOS/Linux filesystem semantics.

### Phase 3: Receive jobs and staging

- Replace direct real pull with durable receive jobs.
- Stage on the destination filesystem, verify content, persist job state, and
  safely resume/discard partial downloads.
- Revalidate source and destination before review/apply.

**Gate:** killing the daemon/rclone during staging never changes destination
files; restart discovers and resumes or safely reports the job.

### Phase 4: Checkpointed apply and rollback

- Implement missing, keep-local, keep-both, selected replacement, selected
  deletion, and leave-staged actions.
- Add atomic per-path journal transitions and startup reconciliation.
- Add conditional rollback and no-auto-cleanup dogfood policy.
- Block related backups only during incomplete apply/reconcile.

**Gate:** interruption at every journal transition is recoverable; replacement
always preserves the prior local version; rollback never overwrites later edits.

### Phase 5: Backups and selective-transfer UI

- Replace raw remote presentation with Computer -> Folder -> Contents.
- Add structured compare results, selection, job progress, review, checkpoint,
  and rollback surfaces.
- Preserve exact CLI/job transparency.

**Gate:** a user can select remote items, compare, stage, approve, reveal, and
roll back without reading a raw rclone path or risking unapproved overwrite.

### Phase 6: Clone

- Add clone-to-new/empty-destination flow.
- Verify and commit the complete folder by directory rename when possible.
- Create a new local identity and optionally add it as a watched folder only
  after explicit confirmation.

**Gate:** clone never activates peer ownership, never merges into a non-empty
destination without review, and produces a verified independent local folder.

### Phase 7: Granular linked folders

- Add manual root/subpath link creation and initial baseline workflow.
- Detect local watcher and peer-generation changes.
- Show one debounced notification per peer generation.
- Reuse compare/receive/apply for Review & Sync.
- Advance baselines and suppress content-equal echo prompts.

**Gate:** `Projects/my-cool-app` links independently of sibling folders; no
cross-computer file change happens before approval; simultaneous edits and
delete-versus-modify become visible conflicts; accepted changes do not loop.

### Phase 8: History and revision recovery

- Keep no app-owned remote trash or snapshots.
- Enter durable machine-wide Recovery Mode while continuing to collect local
  watcher changes.
- Open the exact Dropbox backup folder and guide the user through Rewind.
- Export and verify the rewound remote into a new local folder outside watched
  trees, guide undo-Rewind, then verify current remote equals local twice before
  unlocking.

**Gate:** the app locks every backup path, opens the correct folder, guides both
Rewinds, detects remote movement, produces a verified isolated export, survives
restart in every phase, and cannot exit without fresh equality verification.

### Phase 9: Low-latency and retention polish

- Evaluate Dropbox cursor/long-poll change notifications.
- Add configurable checkpoint, staging, manifest, and remote-trash retention
  only after recovery tests prove cleanup eligibility.
- Add text diff helpers and rename suggestions without changing safety rules.

## Cross-Platform Test Matrix

Each applicable phase must cover macOS and Linux, including:

- Same-volume new clone and multi-file merge.
- External/different-volume destination refusal or explicit safe fallback.
- Large file, empty directory, nested selection, and ignored-path behavior.
- Local-only, peer-only, both-same, both-different, add/delete, delete/modify,
  file/directory, case, Unicode, and symlink conflicts.
- Matching, mismatched, and changed filter policies; peer installation
  replacement with a reused `machine_id`.
- Source changes after compare; destination changes after compare.
- Process interruption during every stage/apply/rollback transition.
- Dropbox offline, rate-limit, authorization expiry, and stale registry.
- Automatic backup after a completed merge, with no backup of work/checkpoint
  directories and no prompt echo loop.
- Headless CLI parity with the desktop workflow.

Real Dropbox dogfood must use disposable local folders and an isolated remote
prefix before any important folder is linked.

## Non-Goals for the First Release

- Automatic live two-way file mutation.
- Remotely controlling or writing an offline peer's local filesystem.
- Sharing one machine/profile ownership identity across computers.
- Automatic conflict winners based on newest timestamp.
- Git-aware semantic merge or a general source-control replacement.
- Multiple peer links for the same or overlapping local scope.
- Claiming complete point-in-time folder restoration before manifest and empty
  directory reconstruction gates pass.
- Permanent cleanup without a reviewed retention and recovery policy.

## Documentation Deliverables

As each phase ships, update the single-source user guide so the repository,
desktop Help tab, and `safe-sync help` remain identical. Until a phase ships,
the guide should not advertise it as available.

## External Capabilities Used by the Design

- Rclone copy skips identical files but may replace differing destination
  files, which is why real cross-computer transfer must stage first:
  <https://rclone.org/commands/rclone_copy/>.
- Rclone check compares without changing source or destination and provides
  categorized reports: <https://rclone.org/commands/rclone_check/>.
- Rclone `--backup-dir` preserves files that would be replaced/deleted:
  <https://rclone.org/docs/#backup-dir-dir>.
- Dropbox folder cursors and long polling can report account changes without a
  repeated full listing: <https://dropbox.github.io/dropbox-sdk-js/Dropbox.html>.
- Dropbox exposes file revisions, but native retention depends on the account
  plan: <https://help.dropbox.com/account-settings/data-retention-policy>.
