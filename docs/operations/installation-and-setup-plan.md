# Installation and Setup Plan

## Purpose

Safe Sync must be usable without development mode. Installation, setup,
updates, and removal are product workflows, not developer instructions.

The first supported delivery is a build-from-source install on macOS and Linux.
Binary release installers come after that path has been used successfully on at
least two real machines.

## Supported Modes

### Desktop

Desktop mode installs the CLI, one managed daemon, and the Tauri tray/control
panel. The daemon continues backing up when the UI is closed.

### Headless

Headless mode installs the CLI and one managed daemon only. It must not build
or install the Tauri app, Node dependencies, Rust UI toolchain, or desktop
libraries. It retains every backup, profile, transfer, registry, trash, and
status capability exposed by the CLI.

On Linux, the installer supports an always-on systemd user service. It must
explain that no administrator access is needed for normal desktop/laptop use:
the service starts when its user logs in. Only a server that must sync directly
after boot, before any user login, requires `sudo loginctl enable-linger
<user>` or an administrator-managed system service.

## User Workflows

### Install From Source

```bash
git clone <repository>
cd safe-sync
./install.sh
```

For a server:

```bash
./install.sh --headless
```

The installer is idempotent. Re-running it upgrades the installed Safe Sync
runtime without deleting configuration, Dropbox authorization, profiles,
folders, remote backups, or trash.

### Setup

```bash
safe-sync setup
```

Setup is separate from installation and may be resumed safely. It must:

1. Create or validate `~/.safe-sync/config.json`.
2. Create or select the active machine profile.
3. Select an existing rclone Dropbox remote or create a Safe Sync-managed one.
4. Complete Dropbox authorization.
5. Verify rclone and Dropbox with a preflight request.
6. Add one or more watched folders.
7. Register the profile remotely.
8. Start the daemon and verify a fresh `safe-sync status` response.

Desktop setup is guided from the installed control panel: it begins Dropbox
authorization using the default `dropbox:computer-backups` remote, asks for a
first local folder, runs the same `safe-sync setup --folder ...` verification,
and starts the daemon. It must display `Setup required` before completion, not
`stale` or an error caused by a missing heartbeat.

The CLI remains the complete and supported setup path, and is required for
headless servers. Safe Sync owns its rclone configuration under
`~/.safe-sync/rclone.conf` for new installs.

For CLI setup, `safe-sync connect-dropbox` selects Safe Sync's `dropbox`
remote and all ordinary rclone defaults. `safe-sync connect-dropbox --headless`
asks only for the resulting JSON token; it does not show rclone's
remote/provider menu or create a remote until a token is supplied.
`safe-sync status` is a post-setup health check, not an installation
prerequisite.

### Update

```bash
./install.sh --update
```

Update stops the Safe Sync UI and managed daemon, installs the new runtime,
preserves state and authentication, restarts the daemon, and verifies health.
It must never modify watched folders or Dropbox backup data as part of an
update.

### Uninstall

```bash
./uninstall.sh
./uninstall.sh --purge
```

Normal uninstall stops and removes Safe Sync services, the CLI wrapper, the
desktop app, logs, and app-managed runtime dependencies. It preserves
`~/.safe-sync` configuration and all Dropbox authorization by default.

`--purge` requires an explicit confirmation and may remove Safe Sync-owned
configuration/state. It never deletes remote Dropbox backups or trash.

## Dependency Ownership

Safe Sync owns the rclone version it executes and, for new installations, its
rclone configuration. The installer downloads a pinned, verified rclone binary
for the current macOS/Linux architecture and stores it in Safe Sync's runtime
directory. New configs point rclone at `~/.safe-sync/rclone.conf`, so a new
machine always performs its own Dropbox authorization instead of inheriting a
system rclone token.

Existing Safe Sync configurations created before this ownership model retain
their global rclone config until the user runs `safe-sync rclone config`; that
explicit command migrates them to the Safe Sync-owned config. Uninstall never
removes unrelated system rclone configuration.

Build-from-source prerequisites are separate from runtime dependencies:

- Runtime: Safe Sync backend environment, managed rclone, service files, and
  desktop app when requested.
- Source-build only: Python, Node, Rust, and platform UI build libraries.

The source installer must report missing prerequisites before it stages a
runtime. It deliberately does not invoke Homebrew, apt, or another package
manager: users install OS-owned build tools through their normal system
workflow. The later binary installer removes Node and Rust from the end-user
dependency list.

## Dropbox Authorization

Setup supports two ownership choices:

1. **Safe Sync-managed remote (default for new installs).** Safe Sync keeps a
   dedicated rclone config and remote under `~/.safe-sync`. It may be removed
   only by explicit purge.
2. **Legacy global rclone remote.** Older Safe Sync configs may continue using
   an existing user rclone config until the user explicitly migrates it.

Headless OAuth uses rclone's standard handoff: the server asks for a token,
the user runs `safe-sync rclone authorize dropbox` on a trusted
browser-equipped machine, then pastes the resulting JSON token into
`safe-sync connect-dropbox --headless`. Tokens must not be placed in shell
arguments, logs, or documentation examples.

## Startup Behavior

Installation creates a user-scoped backend service on macOS and Linux. On
desktop installs, the tray UI is also registered for graphical-login startup.
Neither normal workflow needs `sudo` or administrator access.

Linux `systemd --user` services normally begin after the user logs in. For the
uncommon always-on server case, where backup must resume directly after reboot
without an SSH or console login, an administrator enables lingering once:

```bash
sudo loginctl enable-linger "$USER"
```

The supported verification commands are:

```bash
loginctl show-user "$USER" -p Linger
systemctl --user is-enabled safe-sync-daemon.service
systemctl --user is-active safe-sync-daemon.service
```

Expected output for that server configuration is `Linger=yes`, `enabled`, and
`active`. macOS remains user-login based; Safe Sync does not install a
privileged pre-login service.

Linux headless installation also adds a marked, interactive-only Bash snippet
to `~/.bashrc`. It runs `safe-sync login-check` when an SSH or terminal shell
opens, remains silent when healthy, and prints a short recovery command for
setup, stopped, stale, and Dropbox authorization failures. The check reads the
live local daemon API with a short timeout; it never starts another daemon or
contacts Dropbox. Normal uninstall removes the marked snippet.

## Runtime Layout

The final paths must be stable and user-scoped:

```text
~/.safe-sync/                 configuration, managed auth, state
~/.local/share/safe-sync/     app-managed runtime, including rclone
~/.local/state/safe-sync/     locks and live runtime state
~/.local/log/safe-sync/       logs
~/.local/bin/safe-sync        user-facing CLI wrapper
```

macOS desktop installation installs the app under `~/Applications` by default.
Linux source desktop installation installs the executable under
`~/.local/share/safe-sync/ui/` with Applications-menu and desktop-autostart
entries. Release packages for both platforms are later work.

## Safety Rules

- There is at most one daemon and one tray UI per user.
- Update/uninstall never delete watched local files or remote backups.
- Normal uninstall preserves auth/config; destructive cleanup is opt-in.
- Setup and update finish with a health check, not merely a successful command
  exit.
- The UI is optional; every operational action remains available through the
  CLI.

## Delivery Order

1. Document this contract and align current docs with it.
2. Implement source installation for desktop and headless modes.
3. Implement the complete `safe-sync setup` flow, including Dropbox handoff.
4. Implement update and uninstall behavior.
5. Test clean install, update, uninstall, and reinstall on macOS and Ubuntu.
6. Start versioned two-machine real-world testing at `0.1.0-alpha.1`.
7. Fix reliability and workflow issues found during the soak.
8. Polish UI/UX.
9. Build release installers, then add Windows support.
