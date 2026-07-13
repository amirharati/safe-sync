# Dogfood Report - 2026-07-13

A full end-to-end dogfood test was run from an isolated temporary local root.

## Scope

- Temp local root: `/tmp/safe-sync-dogfood.hwmqEw`
- Temp config under that root; removed after the run.
- Dropbox remote: `dropbox:computer-backups/test/codex-dogfood-hwmqEw/test_sync`
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
