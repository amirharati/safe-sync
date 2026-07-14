# Dogfood Report - 2026-07-13

A full end-to-end dogfood test was run from an isolated temporary local root.

## Scope

- Temp local root: `/tmp/safe-sync-dogfood.hwmqEw`
- Temp config under that root; removed after the run.
- Dropbox remote: `dropbox:computer-backups/test/codex-dogfood-hwmqEw/safe-sync-test`
- Dropbox trash path verified: `dropbox:computer-backups/.trash/test/codex-dogfood-hwmqEw/2026-07-13T00-26-48`
- Real sync folders were not touched.

## Verified

- `safe-sync doctor` passed against Dropbox after network access was available.
- Dry-run selected only the expected useful files:
  - `README.md`
  - `src/app.py`
  - `data/results.csv`
  - `models/model.pt`
- Ignored folders were not uploaded:
  - `node_modules/`
  - `.venv/`
  - `dist/`
- Real upload copied the four expected files.
- Edit sync updated `src/app.py` remotely to `print("dogfood v2 edited")`.
- Delete sync removed `models/model.pt` from the live remote.
- Remote trash preserved both:
  - old `src/app.py` with original `print("dogfood v1")` content
  - deleted `models/model.pt` with original content
- Safe Sync status ended `idle` with `last_error: null`.
- Local tests passed: `5 passed`.

## Notes

- The first sandboxed Dropbox preflight failed due blocked DNS. It passed with approved network access.
- Dropbox rate-limited one live content read with a 300-second retry-after; waiting and retrying once succeeded.
- Local temporary files, config, and logs were removed after the test.


# Daemon Watch Dogfood - 2026-07-13

A real daemon-watch test was run from a temporary folder under the Codex workspace with a unique Dropbox path.

## Scope

- Temp local root: `daemon-real-20260713T203447Z/local` under the Codex workspace
- Dropbox remote: `dropbox:computer-backups/test/codex-daemon/daemon-real-20260713T203447Z/local`
- Dropbox trash root: `dropbox:computer-backups/.trash/test/codex-daemon/daemon-real-20260713T203447Z`
- Local temp folder was removed after verification.
- The installed daemon service was stopped after the test.

## Verified

- `safe-sync doctor` passed against Dropbox.
- `safe-sync daemon --once` detected a new file after debounce and uploaded it.
- `node_modules/` content did not appear in the remote.
- Replacing an existing file uploaded the new content and moved the old remote file to trash.
- Updating an existing file uploaded the updated content and moved the previous remote file to trash.
- Deleting the local file removed it from the live remote and moved the latest remote file to trash.

## Trash Evidence

- `2026-07-13T16-40-30/data/file.txt` contained `v1 new file`.
- `2026-07-13T16-41-02/data/file.txt` contained `v2 replaced whole file`.
- `2026-07-13T16-41-14/data/file.txt` contained `v2 replaced whole file` plus `v3 updated append`.

## Notes

- Dropbox returned one 300-second rate-limit retry during remote listing; waiting succeeded.
- The direct temp daemon uses the same process path as the OS service, so `safe-sync status` still reports the installed service state globally. For the real installed config, this is correct; for temporary configs, service state can be unrelated to the direct daemon process.
