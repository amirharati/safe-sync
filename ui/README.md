# Safe Sync UI

This is the Tauri v2 tray UI for Safe Sync.

The UI has two surfaces:

- A compact macOS tray panel for status and common actions.
- A larger cross-platform control panel for settings, computers, folders, and selective transfers.

The macOS tray surface is a borderless native window containing the Tauri WebView. AppKit positions it from the status item's native window coordinates, avoiding both the `NSPopover` arrow and Tauri's unreliable first-show positioning path.

The tray uses `src-tauri/icons/tray-icon.png`, a tightly framed high-contrast version of the Safe Sync mark designed for macOS's 18-point menu-bar rendering. The normal application and installer icons remain separate.

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

- The main control panel window starts hidden.
- A Safe Sync tray item is created.
- Left-click opens or closes the native macOS status panel.
- Right-click shows the native command menu.
- The panel can refresh status, start or stop the backend, run a backup, open logs, and open the control panel.
- Opening the control panel dismisses the quick panel; opening logs keeps it open while the log viewer comes to the front.
- Closing the control panel hides it without quitting the tray app or backend daemon.
