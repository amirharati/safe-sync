# Safe Sync UI

This is the Tauri v2 tray UI for Safe Sync.

Current checkpoint: placeholder tray skeleton only. It does not call `safe-sync` yet.

## Install

```bash
npm install
```

## Checks

```bash
npm run build
cd src-tauri
cargo check
```

## Run During Development

```bash
npm run tauri dev
```

Expected current behavior:

- The main status window starts hidden.
- A Safe Sync tray/menu item is created.
- The tray menu has placeholder items: status, show window, refresh, quit.
- Real backend status/actions are the next checkpoint.
