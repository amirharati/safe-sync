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
