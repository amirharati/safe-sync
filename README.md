# Safe Sync

Safe Sync is a small wrapper around rclone for personal multi-computer file backup and selective transfer.

The goal is not to build a new sync engine. The goal is to make a boring, inspectable workflow that backs up each computer to its own Dropbox folder and lets files be pulled between computers intentionally.

## Core Idea

- Each computer owns one machine identity and one or more remote backup folders.
- Automatic jobs are mostly one-way backup from each configured local folder to that computer's remote folders.
- Cross-computer sharing is selective: discover another computer, then pull or copy a file/folder when needed.
- Each computer publishes its own registry file at `.registry/computers/<machine_id>.json`; no shared registry file is edited by multiple machines.
- Deletes in owned backup folders are allowed only with recoverable trash.
- No tool syncs `.git/` internals.
- Build artifacts, dependency folders, and caches are ignored.
- Data, trained models, notebooks, configs, lockfiles, and experiment results are backed up.
- Metadata preservation is opt-in to avoid needless Dropbox rewrites and rate-limit pressure.

## Docs

- [Product Plan](docs/product/product-plan.md)
- [Roadmap](docs/roadmap.md)
- [Operating Model](docs/operations/operating-model.md)
- [Daemon Design](docs/operations/daemon-design.md)
- [Test Plan](docs/operations/test-plan.md)
- [Dogfood Report](docs/operations/dogfood-report.md)
- [Tauri Tray Workflow](docs/operations/tauri-tray-workflow.md)
- [Installation and Setup Plan](docs/operations/installation-and-setup-plan.md)
- [Decisions](docs/decisions/0001-safe-sync-model.md)

## First Test Folder

Initial development and testing uses:

```text
~/safe-sync-test
```


## Code Layout

```text
bin/safe-sync                 Thin executable launcher only
src/safe_sync/cli.py          CLI commands and rclone guardrails
src/safe_sync/daemon.py       Polling watch daemon state and scan helpers
src/safe_sync/path_filter.py  Watch-event ignore helper
src/safe_sync/service.py      macOS service install/control rendering
ui/                           Tauri tray app workspace
tests/                        Unit tests for daemon state behavior
```

Run the CLI through `bin/safe-sync`; edit implementation code under `src/safe_sync/`.


## Install From Source

Safe Sync currently supports source installation on macOS and Linux. Windows
is intentionally deferred.

### Prerequisites

Safe Sync installs and pins its own runtime copy of `rclone`; do **not** install
rclone separately. A new Safe Sync installation also owns its rclone config at
`~/.safe-sync/rclone.conf`, including its Dropbox token. It never assumes a
system rclone or a pre-existing Dropbox authorization. Source installation does
require the following tools before you run the installer:

| Mode | Supported now | Required tools |
| --- | --- |
| macOS desktop | Yes | Python 3, Node/npm, Rust/cargo, Xcode Command Line Tools, `curl`, `unzip` |
| macOS headless | Yes | Python 3, `curl`, `unzip`, and `shasum` or `sha256sum` |
| Linux headless | Yes | Python 3, `curl`, `unzip`, and `sha256sum` |
| Linux desktop | Yes, source install | Python 3, Node/npm, Rust/cargo, GTK/WebKit/AppIndicator build libraries, `curl`, `unzip` |

On a typical macOS development machine:

```bash
xcode-select --install
brew install python node rust
```

Then confirm the environment before installing:

```bash
python3 --version
npm --version
cargo --version
```

The installer deliberately does not invoke Homebrew, apt, or another package
manager on your behalf. It fails before staging an installation when a required
tool is missing, and tells you which tool to install. This keeps operating
system dependency ownership visible and under your control.

### macOS Desktop: Clean-Machine Steps

1. Install Xcode Command Line Tools and the source-build prerequisites:

   ```bash
   xcode-select --install
   brew install git python node rust
   ```

   Install Homebrew from [brew.sh](https://brew.sh/) first if `brew` is not
   available.

2. Clone the project and install the production desktop app:

   ```bash
   git clone <repository-url>
   cd safe-sync
   ./install.sh
   ```

   This installs the `safe-sync` command in `~/.local/bin`, the managed daemon,
   and `~/Applications/Safe Sync.app`. If your shell cannot find `safe-sync`,
   add `~/.local/bin` to your zsh startup file and open a new terminal:

   ```bash
   echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.zshrc
   ```

3. Authorize Dropbox once. In the interactive Safe Sync-owned rclone config,
   create a remote named `dropbox`, choose Dropbox as its storage type, and
   complete its browser sign-in flow:

   ```bash
   safe-sync rclone config
   ```

4. Choose only the local folders this computer should back up, then verify and
   start the daemon:

   ```bash
   safe-sync setup --remote dropbox:computer-backups --folder ~/projects --folder ~/data
   safe-sync status
   ```

   Do not point a watched folder at your entire Dropbox folder. Each selected
   local folder receives its own backup path under `dropbox:computer-backups`.

### Ubuntu/Debian Desktop: Clean-Machine Steps

1. Install the desktop source-build prerequisites:

   ```bash
   sudo apt update
   sudo apt install -y git python3 curl unzip build-essential pkg-config libwebkit2gtk-4.0-dev libayatana-appindicator3-dev librsvg2-dev
   ```

   Install Node/npm and Rust/cargo through your normal toolchain workflow. The
   Ubuntu VM development setup uses `nvm` for Node and `rustup` for Rust.

2. Clone and install Safe Sync, including its user-scope tray application:

   ```bash
   git clone <repository-url>
   cd safe-sync
   ./install.sh
   ```

   This adds **Safe Sync** to the desktop Applications menu, installs a desktop
   autostart entry, and launches the tray app. The app itself is stored under
   `~/.local/share/safe-sync/ui/`; it is not copied to a macOS-style
   `~/Applications` directory.

3. Ensure your shell can find the installed command:

   The installer adds `~/.local/bin` to your Bash or Zsh startup file when it
   is missing. Open a new terminal after installation; no manual edit is needed.

4. Configure Dropbox and the folders to back up:

   ```bash
   safe-sync rclone config
   safe-sync setup --remote dropbox:computer-backups --folder ~/projects --folder ~/data
   safe-sync status
   ```

   For a server without a browser, run `safe-sync rclone authorize dropbox` on
   a trusted machine with a browser, then follow rclone's token handoff during
   the server's `safe-sync rclone config` flow.

5. A Linux user service normally runs while you are logged in. For an
   always-on server, ask an administrator or run:

   ```bash
   sudo loginctl enable-linger "$USER"
   ```

### Linux Server: Headless Steps

For a server without a desktop session, install only the backend prerequisites:

```bash
sudo apt update
sudo apt install -y git python3 curl unzip
git clone <repository-url>
cd safe-sync
./install.sh --headless
```

Then use the Dropbox and `safe-sync setup` commands from the desktop section
above. Headless installation does not need Node, Rust, GTK, WebKit, or the tray
app.

### UI and CLI Setup

The **first** setup is currently CLI-led because Dropbox authorization is an
rclone interaction. After `safe-sync setup` succeeds, the macOS tray/control
panel can manage settings, profiles, watched folders, backup requests, and
selective transfers. A guided UI onboarding flow is planned; it is not yet a
replacement for the CLI commands above.

From a downloaded/cloned repo:

```bash
cd /path/to/safe-sync
./install.sh
```

For a server with no desktop UI:

```bash
./install.sh --headless
```

The installer stages a user-scoped runtime, installs a checksum-verified
managed rclone binary, installs the `safe-sync` command in `~/.local/bin`, and
installs one user daemon. Desktop installation also builds and installs the
Tauri tray app on macOS. It preserves existing Safe Sync configuration on
repeat install or update:

```bash
./install.sh --update
```

Complete or validate setup after installation:

```bash
safe-sync setup
```

To add an explicit local folder and choose the remote base during setup:

```bash
safe-sync setup --remote dropbox:computer-backups --folder ~/work
```

If the named Dropbox remote does not already exist, run `safe-sync rclone
config` and then rerun setup. `safe-sync rclone` always invokes the exact
rclone binary and dedicated config that Safe Sync installed. For a config
created by an older Safe Sync release, that command also performs the explicit
one-time migration from the global rclone config to `~/.safe-sync/rclone.conf`.

The installer does the following:

1. Creates `~/.safe-sync/config.json` if it does not exist.
2. Stages `bin/`, `src/`, and configuration templates under `~/.local/share/safe-sync`.
3. Downloads and verifies the Safe Sync-managed rclone runtime.
4. Renders and installs the backend launchd (macOS) or systemd user (Linux) service.
5. Starts the daemon when an existing configuration already has watched folders;
   a first install starts it after `safe-sync setup` has added a folder.
6. On macOS desktop installs, builds the production Tauri tray app at `~/Applications/Safe Sync.app` and enables its LaunchAgent.

Set `SAFE_SYNC_INSTALL_UI=0 ./install.sh` for a backend-only install. Set
`SAFE_SYNC_APP_DIR=/Applications ./install.sh` to install the macOS tray app
somewhere else.

Normal uninstall stops services and removes the installed runtime while
preserving config and Dropbox authorization:

```bash
./uninstall.sh
```

`./uninstall.sh --purge` asks for an explicit confirmation before removing
local configuration. Neither uninstall mode changes remote Dropbox backups or
remote trash.

Start the daemon explicitly when needed:

```bash
safe-sync start
```

Check health:

```bash
safe-sync status
safe-sync logs
```

Stop it:

```bash
safe-sync stop
```

Restart it after config changes:

```bash
safe-sync restart
```

Control backend login autostart:

```bash
safe-sync autostart backend status
safe-sync autostart backend enable
safe-sync autostart backend disable
```

Typical healthy macOS states look like:

```text
backend autostart: enabled (running)
backend autostart: enabled (stopped)
backend autostart: disabled (stopped)
```

`enabled` means launchd is allowed to start Safe Sync at login. `running` or `stopped` is the current daemon process state.

## Configuration

The local config lives at:

```text
~/.safe-sync/config.json
```

List configured folders:

```bash
safe-sync folders list
```

Add another local folder to this machine's backup set:

```bash
safe-sync folders add data ~/data_to_backup --label Data
```

Run health check:

```bash
safe-sync doctor
```

Dry-run backup for all enabled folders:

```bash
safe-sync backup --dry-run
```

Dry-run backup for one folder:

```bash
safe-sync backup safe-sync-test --dry-run
```

Run a real backup:

```bash
safe-sync backup
```

List known computers from the remote registry:

```bash
safe-sync computers
```

Migrate an older config, if needed:

```bash
safe-sync migrate-config
```

## Tray UI Development

The tray UI lives under `ui/` and is a Tauri v2 app. The production installer builds and installs the app; development mode is only for local iteration.

Install UI dependencies once:

```bash
cd /path/to/safe-sync/ui
npm install
```

Check the frontend build:

```bash
npm run build
```

Check the Rust/Tauri side:

```bash
cd src-tauri
cargo check
```

Run the tray app during development:

```bash
cd /path/to/safe-sync/ui
npm run tauri dev
```

The UI dependency lockfiles are committed. Generated folders such as `ui/node_modules/`, `ui/dist/`, and `ui/src-tauri/target/` are ignored. Production install uses `npm ci` and `npm run tauri build`.

## Install Internals

Service templates are rendered from `src/safe_sync/service.py`; generated launchd/systemd files are not kept in the repo.

The macOS installer writes:

```text
~/.local/share/safe-sync/current/
~/.local/bin/safe-sync
~/.safe-sync/config.json
~/Library/LaunchAgents/com.safe-sync.daemon.plist     (macOS)
~/Library/LaunchAgents/com.safe-sync.tray.plist       (macOS desktop)
~/.config/systemd/user/safe-sync-daemon.service      (Linux)
~/Applications/Safe Sync.app                          (macOS desktop)
```

Windows support remains backlog. Linux source installation now supports the
tray application, CLI, and systemd user service; release packages are later.

## Test Folder Reminder

Initial development and manual testing should still use `~/safe-sync-test` or another small explicit folder. Do not point Safe Sync at broad folders like `~`, a whole home directory, or an important work root until the folder-specific config is intentionally reviewed.
