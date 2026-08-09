# Decision 0002: Watch Daemon Model

## Status

Accepted and implemented

## Context

The backup CLI works in dry-run mode against `~/safe-sync-test`. The next question is how the daemon should trigger backups.

A simple timer is reliable but wasteful and slow to react. A pure file watcher is responsive but can miss events after sleep/wake, network outages, or watcher crashes. Dropbox can also rate-limit writes, so the daemon must avoid triggering many successive backups.

## Decision

Use a watcher-first daemon with a timer fallback.

The daemon watches the configured local folder for file changes. It does not run a backup for every event. Instead, it coalesces events into batches:

```text
file change -> mark dirty -> wait debounce window -> run one backup
```

If more changes happen during the debounce window, the debounce timer resets.

If changes happen while a backup is already running, the daemon marks `pending=true`. When the current backup finishes, it waits a cooldown period, then runs one more backup if pending changes exist.

A fallback timer runs periodically even if no watcher events were seen.

## State Machine

```text
idle
  change detected -> dirty
  fallback timer due -> syncing

dirty
  more changes -> reset debounce timer
  quiet for debounce_seconds -> syncing

syncing
  more changes -> pending=true
  success and pending=false -> idle
  success and pending=true -> cooldown
  rate limit/error -> backoff

cooldown
  wait min_interval_seconds -> dirty if pending else idle

backoff
  wait rate_limit_backoff_seconds -> dirty if pending/dirty else idle
```

## Defaults

```json
{
  "debounce_seconds": 20,
  "min_interval_seconds": 120,
  "fallback_interval_seconds": 1800,
  "rate_limit_backoff_seconds": 300
}
```

## Rate Limit Policy

If rclone output includes Dropbox rate-limit messages such as `too_many_requests` or `Trying again in 300 seconds`, Safe Sync should:

- Stop treating the run as a normal success.
- Mark status as `backoff`.
- Avoid immediate retries.
- Keep collecting changes while waiting.
- Run one coalesced backup after backoff expires.

## Watch Backend

The installed runtime owns a pinned, checksum-verified Python `watchdog`
dependency. It maps to FSEvents on macOS and inotify on Linux. Source-tree
development may run without that dependency, but installed macOS/Linux
runtimes include it rather than depending on a global Python package.

If the native backend cannot start, the daemon falls back to full-tree polling
and reports `watcher: polling` plus the startup warning in status and logs.
The normal installed mode reports `watcher: native`.

## Non-Goals

- Track every changed file individually.
- Implement custom sync logic.
- Replace rclone comparison.
- Sync excluded build/cache/dependency folders.

## Consequences

This design is responsive without hammering Dropbox. It also remains robust if watcher events are missed, because the fallback timer eventually runs a backup.
