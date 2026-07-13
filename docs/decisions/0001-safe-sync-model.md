# Decision 0001: Safe Sync Model

## Status

Accepted

## Context

Two-way live sync across several computers and Dropbox is fragile for a broad development folder. The current workflow includes source code, data, notebooks, trained model files, and generated build/cache/dependency artifacts. Some files are also tracked by Git.

The important requirement is not "perfect live sync." The important requirement is that files are backed up, available across machines when needed, and not silently deleted or overwritten because one machine or wrapper got confused.

## Decision

Use a per-computer backup model plus explicit selective transfer.

Each computer backs up its local project folder to its own remote folder:

```text
dropbox:computer-backups/<machine>/projects
```

Examples:

```text
Mac    -> dropbox:computer-backups/macbook/projects
Linux  -> dropbox:computer-backups/linuxbox/projects
Windows -> dropbox:computer-backups/windowsbox/projects
```

Cross-computer transfer is explicit:

```bash
safe-sync pull linuxbox projects/my_exp ~/projects/from-linux/my_exp
safe-sync push-shared ~/projects/report shared/report
```

Automatic live two-way sync is reserved only for small folders that truly need it.

## Safety Rules

- Do not sync `.git/`.
- Do not delete files across computers automatically.
- Automatic backup may mirror local deletes into that machine's remote backup, but only with `--backup-dir` trash.
- Pull/copy operations do not delete destination files by default.
- Conflicts produce renamed files rather than overwriting silently.
- Rclone remains the sync/copy engine; Safe Sync only provides guardrails, status, and workflow commands.

## Consequences

This model is less magical than a shared live folder, but it is easier to trust.

Benefits:

- A bad delete on one computer does not delete another computer's backup.
- Remote backup folders are easy to inspect.
- Recovery preserves original relative paths.
- Git working trees remain normal working trees.
- The system can be implemented incrementally.

Tradeoffs:

- Some cross-computer movement is explicit instead of automatic.
- Remote trash can grow and needs cleanup policy.
- There may be duplicate copies between machine backups.

