import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { getCurrentWindow } from "@tauri-apps/api/window";
import "./styles.css";

type SafeSyncStatus = {
  health: string;
  health_reason: string;
  service_state: string;
  sync_state: Record<string, unknown>;
  daemon_seen_at: string | null;
  log: string | null;
};

type SafeSyncConfig = {
  config_path: string;
  profile_id: string | null;
  profile_label: string | null;
  active_profile_id: string | null;
  machine_id: string | null;
  machine_label: string | null;
  remote_base: string | null;
  rclone_config: string | null;
  poll_interval_seconds: number;
  debounce_seconds: number;
  min_interval_seconds: number;
  fallback_interval_seconds: number;
  rate_limit_backoff_seconds: number;
  folders: Array<Record<string, unknown>>;
  profiles: Array<Record<string, unknown>>;
};

type CommandResult = { ok: boolean; output: string };
type DropboxConnection = { connected: boolean; output: string };
type LocalFolderPreview = {
  path: string;
  exists: boolean;
  entries: string[];
  truncated: boolean;
};

type FolderView = Record<string, unknown> & { id?: string; label?: string; local_path?: string; remote_root?: string; enabled?: boolean };
type ProfileView = Record<string, unknown> & {
  id?: string;
  label?: string;
  machine_id?: string;
  machine_label?: string;
  remote_base?: string;
  active?: boolean;
  folder_count?: number;
};
type ComputerView = Record<string, unknown> & {
  machine_id?: string;
  machine_label?: string;
  machine?: string;
  generated_at?: string;
  updated_at?: string;
  folders?: unknown[];
};

const IDLE_REFRESH_MS = 10_000;
const ACTIVE_REFRESH_MS = 1_500;
const ACTION_FEEDBACK_MS = 1800;
const IS_QUICK_PANEL = getCurrentWindow().label === "quick";

const stateLabel = document.querySelector<HTMLElement>("[data-status-state]");
const reasonLabel = document.querySelector<HTMLElement>("[data-status-reason]");
const serviceLabel = document.querySelector<HTMLElement>("[data-service-state]");
const syncLabel = document.querySelector<HTMLElement>("[data-sync-state]");
const currentFolderLabel = document.querySelector<HTMLElement>("[data-current-folder]");
const currentProgressLabel = document.querySelector<HTMLElement>("[data-current-progress]");
const currentFileLabel = document.querySelector<HTMLElement>("[data-current-file]");
const activityList = document.querySelector<HTMLElement>("[data-activity-list]");
const seenLabel = document.querySelector<HTMLElement>("[data-daemon-seen]");
const logLabel = document.querySelector<HTMLElement>("[data-log-path]");
const refreshLabel = document.querySelector<HTMLElement>("[data-refresh-note]");
const statusDot = document.querySelector<HTMLElement>("[data-status-dot]");
const message = document.querySelector<HTMLElement>("[data-message]");
const refreshButton = document.querySelector<HTMLButtonElement>("[data-action='refresh']");
const toggleButton = document.querySelector<HTMLButtonElement>("[data-action='toggle-backend']");
const backupButton = document.querySelector<HTMLButtonElement>("[data-action='backup-now']");
const logsButton = document.querySelector<HTMLButtonElement>("[data-action='open-logs']");
const configPath = document.querySelector<HTMLElement>("[data-config-path]");
const profileId = document.querySelector<HTMLElement>("[data-profile-id]");
const machineId = document.querySelector<HTMLElement>("[data-machine-id]");
const remoteBase = document.querySelector<HTMLElement>("[data-remote-base]");
const settingsForm = document.querySelector<HTMLFormElement>("[data-settings-form]");
const addFolderForm = document.querySelector<HTMLFormElement>("[data-add-folder-form]");
const folderList = document.querySelector<HTMLElement>("[data-folder-list]");
const profileList = document.querySelector<HTMLElement>("[data-profile-list]");
const addProfileForm = document.querySelector<HTMLFormElement>("[data-add-profile-form]");
const localComputerList = document.querySelector<HTMLElement>("[data-local-computer-list]");
const computerList = document.querySelector<HTMLElement>("[data-computer-list]");
const transferForm = document.querySelector<HTMLFormElement>("[data-transfer-form]");
const transferOutput = document.querySelector<HTMLElement>("[data-transfer-output]");
const transferBrowser = document.querySelector<HTMLElement>("[data-transfer-browser]");
const transferEntryList = document.querySelector<HTMLElement>("[data-transfer-entry-list]");
const transferSelectedSource = document.querySelector<HTMLElement>("[data-transfer-selected-source]");
const transferCommand = document.querySelector<HTMLElement>("[data-transfer-command]");
const transferLiveState = document.querySelector<HTMLElement>("[data-transfer-live-state]");
const transferLiveSummary = document.querySelector<HTMLElement>("[data-transfer-live-summary]");
const transferActivityList = document.querySelector<HTMLElement>("[data-transfer-activity-list]");
const transferPreview = document.querySelector<HTMLElement>("[data-transfer-preview]");
const previewSourcePath = document.querySelector<HTMLElement>("[data-preview-source-path]");
const previewSourceList = document.querySelector<HTMLElement>("[data-preview-source-list]");
const previewDestinationPath = document.querySelector<HTMLElement>("[data-preview-destination-path]");
const previewDestinationList = document.querySelector<HTMLElement>("[data-preview-destination-list]");
const transferSelection = document.querySelector<HTMLElement>("[data-transfer-selection]");
const transferSelectionList = document.querySelector<HTMLElement>("[data-transfer-selection-list]");
const lastCommand = document.querySelector<HTMLElement>("[data-last-command]");
const setupPanel = document.querySelector<HTMLElement>("[data-setup-panel]");
const setupForm = document.querySelector<HTMLFormElement>("[data-setup-form]");
const dropboxConnectionLabel = document.querySelector<HTMLElement>("[data-dropbox-connection]");
const connectDropboxButton = document.querySelector<HTMLButtonElement>("[data-action='connect-dropbox']");
const completeSetupButton = document.querySelector<HTMLButtonElement>("[data-action='complete-setup']");

let latestStatus: SafeSyncStatus | null = null;
let busyAction: string | null = null;
let feedbackAction: string | null = null;
let feedbackTimer: number | null = null;
let refreshTimer: number | null = null;
let statusRefreshInFlight = false;
let configLoaded = false;
let computersLoaded = false;
let latestConfig: SafeSyncConfig | null = null;
let latestComputers: Array<Record<string, unknown>> = [];
let transferSourceRoot = "";
let transferSource = "";
let transferSourceIsDirectory = true;
const selectedTransferPaths = new Set<string>();
let lastUiCommand = "";
let dropboxConnectionKnown = false;
let dropboxConnected = false;

function text(value: unknown, fallback = "-"): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function setMessage(value: string, tone = "neutral"): void {
  if (message) {
    message.textContent = value;
    message.dataset.tone = tone;
  }
}

function renderDropboxConnection(connected: boolean): void {
  dropboxConnectionKnown = true;
  dropboxConnected = connected;
  if (dropboxConnectionLabel) {
    dropboxConnectionLabel.textContent = connected ? "Dropbox connected" : "Connect Dropbox before finishing setup";
    dropboxConnectionLabel.dataset.connected = String(connected);
  }
  if (connectDropboxButton) {
    connectDropboxButton.textContent = connected ? "Dropbox Connected" : "Connect Dropbox";
    connectDropboxButton.disabled = connected;
  }
  if (completeSetupButton) completeSetupButton.disabled = !connected;
}

async function refreshDropboxConnection(): Promise<void> {
  try {
    const connection = await invoke<DropboxConnection>("get_dropbox_connection");
    renderDropboxConnection(connection.connected);
  } catch (error) {
    if (dropboxConnectionLabel) {
      dropboxConnectionLabel.textContent = `Dropbox check failed: ${String(error)}`;
      dropboxConnectionLabel.dataset.connected = "false";
    }
  }
}

function syncState(status: SafeSyncStatus): string {
  return text(status.sync_state?.state);
}

function tone(status: SafeSyncStatus): string {
  if (status.health === "setup_required") return "warning";
  if (status.health === "error") return "error";
  if (status.health === "warning") return "warning";
  if (status.service_state === "stopped") return "stopped";
  if (status.health === "stale") return "stale";
  if (["syncing", "transferring", "dirty", "cooldown", "backoff"].includes(syncState(status))) return "active";
  if (status.health === "ok") return "ok";
  return "unknown";
}

function headline(status: SafeSyncStatus): string {
  if (status.health === "setup_required") return "Setup required";
  if (status.health === "error") return "Needs attention";
  if (status.health === "warning") return syncState(status) === "backoff" ? "Waiting" : "Warning";
  if (status.service_state === "stopped") return "Stopped";
  const currentSyncState = syncState(status);
  if (currentSyncState === "syncing") return "Syncing";
  if (currentSyncState === "transferring") return "Transferring";
  if (currentSyncState === "dirty") return "Changes queued";
  if (currentSyncState === "cooldown") return "Cooling down";
  if (currentSyncState === "backoff") return "Waiting";
  if (status.health === "ok") return "Watching";
  return text(status.health, "Unknown");
}

function desiredAction(status: SafeSyncStatus): "start" | "stop" {
  return status.service_state === "running" ? "stop" : "start";
}

function currentFolderSummary(status: SafeSyncStatus): string {
  const syncStateValue = syncState(status);
  const folderId = text(status.sync_state?.folder_id, "");
  const folderLabel = text(status.sync_state?.current_folder_label, folderId);
  const index = Number(status.sync_state?.current_folder_index ?? 0);
  const total = Number(status.sync_state?.current_folder_total ?? 0);
  if (!folderLabel) return "-";
  if (index > 0 && total > 0) {
    return `${folderLabel} (${index}/${total})`;
  }
  if (syncStateValue === "syncing" || syncStateValue === "transferring" || syncStateValue === "backoff" || syncStateValue === "cooldown") {
    return folderLabel;
  }
  return folderLabel;
}

function progressSummary(status: SafeSyncStatus): string {
  const live = text(status.sync_state?.last_progress, "");
  if (live) return live;
  const syncStateValue = syncState(status);
  const backoff = Number(status.sync_state?.backoff_remaining_seconds ?? 0);
  if (syncStateValue === "backoff" && backoff > 0) return `Retrying in ${Math.ceil(backoff)}s`;
  const cooldown = Number(status.sync_state?.cooldown_remaining_seconds ?? 0);
  if (syncStateValue === "cooldown" && cooldown > 0) return `Waiting ${Math.ceil(cooldown)}s before next sync`;
  if (syncStateValue === "dirty") return "Changes queued";
  if (syncStateValue === "watching") return "Watching for changes";
  return "-";
}

function currentFileSummary(status: SafeSyncStatus): string {
  const currentFile = text(status.sync_state?.current_file, "");
  if (currentFile) return currentFile;
  if (["syncing", "transferring"].includes(syncState(status))) return "Waiting for file detail";
  return "-";
}

function activityItems(status: SafeSyncStatus): string[] {
  const raw = status.sync_state?.recent_activity;
  if (!Array.isArray(raw)) return [];
  return raw.filter((entry): entry is string => typeof entry === "string" && entry.length > 0).slice(0, 6);
}

function hasLog(status: SafeSyncStatus | null): boolean {
  return Boolean(status?.log && status.log.length > 0);
}

function actionNameForButton(button: HTMLButtonElement): string | null {
  const action = button.dataset.action;
  if (action === "backup-now") return "backup";
  if (action === "toggle-backend") return "backend";
  if (action === "open-logs") return "logs";
  if (action === "open-control-panel") return "panel";
  if (action === "close-quick") return "close";
  if (action === "quit-tray") return "quit";
  if (action === "settings") return "settings";
  if (action === "connect-dropbox") return "dropbox-connect";
  if (action === "complete-setup") return "setup";
  if (action === "pick-setup-folder") return "setup-picker";
  if (action === "reload-config") return "config";
  if (action === "pick-folder") return "folder-picker";
  if (action === "pick-transfer-destination") return "transfer-picker";
  if (action === "open-source-local" || action === "open-destination-local") return "open-local";
  if (action === "open-source-dropbox" || action === "open-destination-dropbox") return "open-dropbox";
  if (action === "open-dropbox") return "dropbox";
  if (action === "activate-profile") return "profile";
  if (action === "remove-folder") return "folder";
  if (action === "load-computers") return "computers";
  if (action === "list-remote") return "transfer";
  if (action === "preview-transfer") return "transfer-preview";
  if (action === "run-transfer") return "transfer";
  if (action === "refresh-transfer") return "transfer";
  return action ?? null;
}

function holdAction(action: string): void {
  if (feedbackTimer !== null) window.clearTimeout(feedbackTimer);
  feedbackAction = action;
  setBusy(null);
  feedbackTimer = window.setTimeout(() => {
    feedbackAction = null;
    feedbackTimer = null;
    setBusy(null);
  }, ACTION_FEEDBACK_MS);
}

function isHeld(action: string): boolean {
  return feedbackAction === action;
}

function setBusy(action: string | null): void {
  busyAction = action;
  for (const button of document.querySelectorAll<HTMLButtonElement>("button")) {
    const isFeedback = feedbackAction !== null && actionNameForButton(button) === feedbackAction;
    const isCurrentAction = action !== null && actionNameForButton(button) === action;
    button.disabled = isCurrentAction || isFeedback;
    button.dataset.feedback = isFeedback ? "true" : "false";
  }
  if (refreshButton) refreshButton.disabled = action === "refresh" || isHeld("refresh");
  if (toggleButton) toggleButton.disabled = action === "backend" || isHeld("backend") || latestStatus?.service_state === "unknown";
  if (backupButton) {
    backupButton.disabled = action === "backup" || isHeld("backup") || latestStatus?.service_state !== "running" || latestStatus?.health === "setup_required";
    backupButton.textContent = action === "backup" ? "Backing Up" : "Backup Now";
    backupButton.title = latestStatus?.service_state === "running" ? "" : "Start the backend before running Backup Now";
  }
  if (logsButton) logsButton.disabled = action === "logs" || isHeld("logs") || !hasLog(latestStatus);
  if (connectDropboxButton && dropboxConnected) connectDropboxButton.disabled = true;
  if (completeSetupButton) completeSetupButton.disabled = action === "setup" || isHeld("setup") || !dropboxConnected;
}

function renderStatus(status: SafeSyncStatus): void {
  latestStatus = status;
  const currentTone = tone(status);
  const currentHeadline = headline(status);
  const action = desiredAction(status);
  document.documentElement.dataset.statusTone = currentTone;
  if (statusDot) {
    statusDot.dataset.tone = currentTone;
    statusDot.setAttribute("aria-label", currentHeadline);
  }
  if (stateLabel) {
    stateLabel.textContent = currentHeadline;
    stateLabel.dataset.health = currentTone;
  }
  if (reasonLabel) reasonLabel.textContent = text(status.health_reason);
  if (setupPanel) setupPanel.hidden = status.health !== "setup_required";
  if (status.health === "setup_required" && !dropboxConnectionKnown) void refreshDropboxConnection();
  if (serviceLabel) {
    serviceLabel.textContent = text(status.service_state);
    serviceLabel.dataset.value = status.service_state;
  }
  if (syncLabel) syncLabel.textContent = syncState(status);
  if (currentFolderLabel) currentFolderLabel.textContent = currentFolderSummary(status);
  if (currentProgressLabel) currentProgressLabel.textContent = progressSummary(status);
  if (currentFileLabel) currentFileLabel.textContent = currentFileSummary(status);
  if (seenLabel) seenLabel.textContent = text(status.daemon_seen_at);
  if (logLabel) logLabel.textContent = text(status.log);
  if (activityList) {
    const items = activityItems(status);
    activityList.innerHTML = "";
    if (items.length === 0) {
      const item = document.createElement("li");
      item.className = "activity-empty";
      item.textContent = syncState(status) === "syncing" ? "Waiting for first file event" : "No recent file activity";
      activityList.append(item);
    } else {
      for (const entry of items) {
        const item = document.createElement("li");
        item.className = "activity-item";
        item.textContent = entry;
        activityList.append(item);
      }
    }
  }
  renderTransferActivity(status);
  const refreshMs = ["syncing", "transferring", "dirty", "cooldown", "backoff"].includes(syncState(status))
    ? ACTIVE_REFRESH_MS
    : IDLE_REFRESH_MS;
  if (refreshLabel) refreshLabel.textContent = `Auto refresh every ${refreshMs / 1000}s`;
  if (toggleButton) {
    toggleButton.textContent = status.health === "setup_required" ? "Complete Setup" : action === "stop" ? "Stop Backend" : "Start Backend";
    toggleButton.dataset.intent = action;
  }
  setBusy(busyAction);
  scheduleStatusRefresh();
}

function renderTransferActivity(status: SafeSyncStatus): void {
  const state = syncState(status);
  const progress = progressSummary(status);
  const command = text(status.sync_state?.last_command, "");
  const queued = status.sync_state?.queued_transfer === true;
  if (transferLiveState) {
    transferLiveState.textContent = state === "transferring" ? "Transferring" : queued ? "Queued" : state === "syncing" ? "Backup running" : state === "watching" ? "Waiting" : state;
    transferLiveState.classList.toggle("is-active", state === "transferring");
  }
  if (transferLiveSummary) {
    if (state === "transferring") {
      transferLiveSummary.textContent = `${text(status.sync_state?.source, "Remote source")} -> ${text(status.sync_state?.destination, "Local destination")}\n${progress}`;
    } else if (queued) {
      transferLiveSummary.textContent = state === "backoff"
        ? "Queued until the Dropbox cooldown ends."
        : "Queued behind the current backup. It will begin automatically.";
    } else if (command === "pull") {
      transferLiveSummary.textContent = progress;
    } else {
      transferLiveSummary.textContent = "Transfers are queued behind any active backup and run one at a time.";
    }
  }
  if (transferActivityList) {
    transferActivityList.innerHTML = "";
    const items = activityItems(status);
    if (items.length === 0) {
      const item = document.createElement("li");
      item.className = "activity-empty";
      item.textContent = "No transfer activity yet.";
      transferActivityList.append(item);
    } else {
      for (const entry of items) {
        const item = document.createElement("li");
        item.className = "activity-item";
        item.textContent = entry;
        transferActivityList.append(item);
      }
    }
  }
}

function renderError(error: unknown): void {
  renderStatus({
    health: "error",
    health_reason: String(error),
    service_state: "unknown",
    sync_state: {},
    daemon_seen_at: null,
    log: null,
  });
}

function inputValue(form: HTMLFormElement, name: string): string {
  const field = form.elements.namedItem(name) as HTMLInputElement | null;
  return field?.value.trim() ?? "";
}

function escapeHtml(value: string): string {
  return value.replace(/[&<>"']/g, (char) => {
    const map: Record<string, string> = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
    return map[char];
  });
}

function numberValue(form: HTMLFormElement, name: string): number {
  return Number(inputValue(form, name));
}

function dropboxUrl(remoteRoot: string): string | null {
  if (!remoteRoot.startsWith("dropbox:")) return null;
  const rawPath = remoteRoot.slice("dropbox:".length).replace(/^\/+/, "");
  if (!rawPath) return null;
  const encoded = rawPath
    .split("/")
    .filter((segment) => segment.length > 0)
    .map((segment) => encodeURIComponent(segment))
    .join("/");
  return `https://www.dropbox.com/home/${encoded}`;
}

function formField(form: HTMLFormElement, name: string): HTMLInputElement | HTMLSelectElement | null {
  return form.elements.namedItem(name) as HTMLInputElement | HTMLSelectElement | null;
}

function selectedValue(form: HTMLFormElement, name: string): string {
  return formField(form, name)?.value.trim() ?? "";
}

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, "'\\\"'\\\"'")}'`;
}

function showUiCommand(args: string[]): void {
  lastUiCommand = `safe-sync ${args.map(shellQuote).join(" ")}`;
  if (lastCommand) lastCommand.textContent = lastUiCommand;
}

function remoteRoot(remoteBase: string, remotePath: string): string {
  if (remotePath.startsWith("dropbox:")) return remotePath;
  const base = remoteBase.replace(/\/+$/, "");
  const bareBase = base.replace(/^[^:]+:/, "").replace(/^\/+/, "");
  const barePath = remotePath.replace(/^\/+/, "");
  if (barePath === bareBase || barePath.startsWith(`${bareBase}/`)) return `dropbox:${barePath}`;
  return `${base}/${barePath}`;
}

function cleanSubfolder(value: string): string | null {
  const cleaned = value.trim().replace(/^[/\\]+|[/\\]+$/g, "");
  if (!cleaned) return "";
  if (cleaned.split(/[\\/]+/).some((part) => part === "..")) return null;
  return cleaned;
}

function joinLocalPath(base: string, subfolder: string): string {
  return subfolder ? `${base.replace(/[\\/]$/, "")}/${subfolder}` : base;
}

function remoteSourceName(source: string): string {
  const cleaned = source.replace(/[\\/]+$/, "");
  const separator = Math.max(cleaned.lastIndexOf("/"), cleaned.lastIndexOf("\\"));
  return separator >= 0 ? cleaned.slice(separator + 1) : cleaned;
}

function transferDestination(): string | null {
  if (!transferForm) return null;
  const base = selectedValue(transferForm, "destination_path");
  if (!base) return null;
  const subfolder = cleanSubfolder(selectedValue(transferForm, "destination_subfolder"));
  if (subfolder === null) return null;
  const parent = joinLocalPath(base, subfolder);
  // rclone copies a directory's contents. For an arbitrary destination, add its
  // source name so a remote `assets` folder becomes `Documents/assets`.
  if (transferSourceIsDirectory && !selectedDestinationFolder()) {
    const name = remoteSourceName(transferSourceRoot);
    return name ? joinLocalPath(parent, name) : parent;
  }
  return parent;
}

function selectedDestinationFolder(): FolderView | null {
  if (!transferForm || !latestConfig) return null;
  const id = selectedValue(transferForm, "destination_folder");
  return (latestConfig.folders.find((raw) => text((raw as FolderView).id, "") === id) as FolderView | undefined) ?? null;
}

function remotePathForDestination(): string | null {
  const folder = selectedDestinationFolder();
  if (!folder?.local_path || !folder.remote_root || !transferForm) return null;
  if (selectedValue(transferForm, "destination_path") !== folder.local_path) return null;
  const subfolder = cleanSubfolder(selectedValue(transferForm, "destination_subfolder"));
  return subfolder === null ? null : subfolder ? `${folder.remote_root.replace(/\/+$/, "")}/${subfolder}` : folder.remote_root;
}

function localPathForSource(): string | null {
  if (!latestConfig || !transferSource) return null;
  for (const raw of latestConfig.folders) {
    const folder = raw as FolderView;
    if (!folder.local_path || !folder.remote_root) continue;
    const root = folder.remote_root.replace(/\/+$/, "");
    if (transferSource === root) return folder.local_path;
    if (transferSource.startsWith(`${root}/`)) return joinLocalPath(folder.local_path, transferSource.slice(root.length + 1));
  }
  return null;
}

function updateTransferLocationActions(): void {
  const sourceLocal = document.querySelector<HTMLButtonElement>("[data-action='open-source-local']");
  const destinationLocal = document.querySelector<HTMLButtonElement>("[data-action='open-destination-local']");
  const destinationDropbox = document.querySelector<HTMLButtonElement>("[data-action='open-destination-dropbox']");
  if (sourceLocal) sourceLocal.disabled = !localPathForSource();
  if (destinationLocal) destinationLocal.disabled = !transferDestination();
  if (destinationDropbox) destinationDropbox.disabled = !remotePathForDestination();
}

function updateTransferCommand(): void {
  if (!transferCommand || !transferForm) return;
  const destination = transferDestination();
  const subfolder = cleanSubfolder(selectedValue(transferForm, "destination_subfolder"));
  const dryRun = (transferForm.elements.namedItem("dry_run") as HTMLInputElement | null)?.checked ?? true;
  const runButton = document.querySelector<HTMLButtonElement>("[data-action='run-transfer']");
  if (runButton) runButton.textContent = dryRun ? "Run Dry Run" : "Copy To Local Folder";
  if (subfolder === null) {
    transferCommand.textContent = "Destination subfolder cannot contain ..";
  } else if (!transferSource || !destination) {
    transferCommand.textContent = "Choose a source and destination folder.";
  } else {
    const selected = [...selectedTransferPaths].map((path) => ` --select ${shellQuote(path)}`).join("");
    transferCommand.textContent = `safe-sync pull ${shellQuote(transferSourceRoot)} ${shellQuote(destination)}${dryRun ? " --dry-run" : ""}${selected}`;
  }
  updateTransferLocationActions();
}

function sourceComputer(): ComputerView | null {
  if (!transferForm) return null;
  const machineId = selectedValue(transferForm, "source_computer");
  return remoteComputerByMachineId(machineId);
}

function renderTransferOptions(): void {
  if (!transferForm) return;
  const computerSelect = formField(transferForm, "source_computer") as HTMLSelectElement | null;
  const sourceFolderSelect = formField(transferForm, "source_folder") as HTMLSelectElement | null;
  const destinationSelect = formField(transferForm, "destination_folder") as HTMLSelectElement | null;
  const destinationPath = formField(transferForm, "destination_path") as HTMLInputElement | null;
  if (!computerSelect || !sourceFolderSelect || !destinationSelect || !destinationPath) return;

  const priorComputer = computerSelect.value;
  const priorFolder = sourceFolderSelect.value;
  const priorDestination = destinationSelect.value;
  computerSelect.innerHTML = "";
  for (const raw of latestComputers) {
    const computer = raw as ComputerView;
    const machineId = text(computer.machine_id, text(computer.machine, ""));
    if (!machineId || !Array.isArray(computer.folders) || computer.folders.length === 0) continue;
    const option = document.createElement("option");
    option.value = machineId;
    option.textContent = text(computer.machine_label, machineId);
    computerSelect.append(option);
  }
  if (priorComputer && [...computerSelect.options].some((option) => option.value === priorComputer)) computerSelect.value = priorComputer;

  const renderSourceFolders = (): void => {
    const computer = sourceComputer();
    sourceFolderSelect.innerHTML = "";
    const folders = Array.isArray(computer?.folders) ? computer.folders : [];
    for (const raw of folders) {
      const folder = raw as Record<string, unknown>;
      const remotePath = text(folder.remote_path, "");
      if (!remotePath) continue;
      const option = document.createElement("option");
      option.value = remoteRoot(latestConfig?.remote_base ?? "dropbox:", remotePath);
      option.textContent = text(folder.label, text(folder.id, remotePath));
      sourceFolderSelect.append(option);
    }
    if (priorFolder && [...sourceFolderSelect.options].some((option) => option.value === priorFolder)) sourceFolderSelect.value = priorFolder;
    transferSourceRoot = sourceFolderSelect.value;
    transferSource = transferSourceRoot;
    transferSourceIsDirectory = true;
    selectedTransferPaths.clear();
    renderTransferSelection();
  };
  renderSourceFolders();
  computerSelect.onchange = () => {
    renderSourceFolders();
    hideTransferBrowser();
    updateTransferCommand();
  };
  sourceFolderSelect.onchange = () => {
    transferSourceRoot = sourceFolderSelect.value;
    transferSource = transferSourceRoot;
    transferSourceIsDirectory = true;
    selectedTransferPaths.clear();
    renderTransferSelection();
    hideTransferBrowser();
    updateTransferCommand();
  };

  destinationSelect.innerHTML = "";
  const customDestination = document.createElement("option");
  customDestination.value = "";
  customDestination.textContent = "Choose any local folder";
  destinationSelect.append(customDestination);
  for (const raw of latestConfig?.folders ?? []) {
    const folder = raw as FolderView;
    if (folder.enabled === false || !folder.id || !folder.local_path) continue;
    const option = document.createElement("option");
    option.value = folder.id;
    option.textContent = `${text(folder.label, folder.id)} - ${folder.local_path}`;
    destinationSelect.append(option);
  }
  if (priorDestination && [...destinationSelect.options].some((option) => option.value === priorDestination)) destinationSelect.value = priorDestination;
  const selectedFolder = selectedDestinationFolder();
  if (!destinationPath.value && selectedFolder?.local_path) destinationPath.value = selectedFolder.local_path;
  destinationSelect.onchange = () => {
    const folder = selectedDestinationFolder();
    if (folder?.local_path) {
      destinationPath.value = folder.local_path;
    } else {
      destinationPath.value = "";
    }
    updateTransferCommand();
  };
  updateTransferCommand();
}

function hideTransferBrowser(): void {
  if (transferBrowser) transferBrowser.hidden = true;
  if (transferEntryList) transferEntryList.innerHTML = "";
}

function relativeTransferPath(path: string): string | null {
  const root = transferSourceRoot.replace(/\/+$/, "");
  if (!root || !path.startsWith(`${root}/`)) return null;
  return path.slice(root.length + 1);
}

function renderTransferSelection(): void {
  if (!transferSelection || !transferSelectionList) return;
  transferSelectionList.innerHTML = "";
  for (const path of [...selectedTransferPaths].sort((left, right) => left.localeCompare(right))) {
    const item = document.createElement("li");
    const label = document.createElement("span");
    label.textContent = path;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "secondary";
    remove.dataset.action = "remove-transfer-entry";
    remove.dataset.path = path;
    remove.textContent = "Remove";
    item.append(label, remove);
    transferSelectionList.append(item);
  }
  transferSelection.hidden = selectedTransferPaths.size === 0;
}

function toggleTransferSelection(path: string): void {
  if (selectedTransferPaths.has(path)) {
    selectedTransferPaths.delete(path);
  } else {
    selectedTransferPaths.add(path);
  }
  renderTransferSelection();
  updateTransferCommand();
}

function renderRemoteEntries(output: string, base: string): void {
  if (!transferBrowser || !transferEntryList || !transferSelectedSource) return;
  const entries = output.split("\n").map((line) => line.trim()).filter(Boolean).slice(0, 200);
  transferSelectedSource.textContent = base;
  transferEntryList.innerHTML = "";
  for (const entry of entries) {
    const directory = entry.endsWith("/");
    const item = document.createElement("article");
    item.className = "item transfer-entry";
    const label = document.createElement("span");
    label.textContent = entry;
    item.append(label);
    if (directory) {
      const open = document.createElement("button");
      open.type = "button";
      open.className = "secondary";
      open.dataset.action = "open-transfer-entry";
      open.dataset.entry = entry;
      open.textContent = "Open";
      item.append(open);
    }
    const select = document.createElement("button");
    select.type = "button";
    select.className = "secondary";
    select.dataset.action = "toggle-transfer-entry";
    select.dataset.entry = entry;
    select.dataset.directory = String(directory);
    select.textContent = directory ? "Add Folder" : "Add File";
    item.append(select);
    transferEntryList.append(item);
  }
  if (entries.length === 0) transferEntryList.textContent = "No files found in this folder.";
  if (output.split("\n").filter(Boolean).length > entries.length) {
    const note = document.createElement("p");
    note.className = "reason";
    note.textContent = "Showing the first 200 entries. Open a folder to narrow the list.";
    transferEntryList.append(note);
  }
  transferBrowser.hidden = false;
}

function remoteComputerByMachineId(machineId: string | undefined | null): ComputerView | null {
  if (!machineId) return null;
  const match = latestComputers.find((entry) => {
    const computer = entry as ComputerView;
    return text(computer.machine_id, text(computer.machine)) === machineId;
  });
  return (match as ComputerView | undefined) ?? null;
}

function renderComputersView(): void {
  if (localComputerList) {
    localComputerList.innerHTML = "";
    const profiles = latestConfig?.profiles ?? [];
    for (const rawProfile of profiles) {
      const profile = rawProfile as ProfileView;
      const machineKey = text(profile.machine_id, text(profile.id, ""));
      const remote = remoteComputerByMachineId(machineKey);
      const item = document.createElement("article");
      item.className = "item";
      item.dataset.profileId = text(profile.id);
      item.innerHTML = `
        <div class="item-heading">
          <strong>${text(profile.label, text(profile.id))}</strong>
          <div class="pill-row">
            <span class="pill ${profile.active ? "is-active" : ""}">${profile.active ? "Active" : "Inactive"}</span>
            <span class="pill ${remote ? "is-linked" : ""}">${remote ? "Published" : "Local only"}</span>
          </div>
        </div>
        <span>${escapeHtml(text(profile.machine_label, text(profile.machine_id)))}</span>
        <span>${Number(profile.folder_count ?? 0)} folder(s)</span>
        <span>${remote ? `Registry: ${escapeHtml(text(remote.updated_at, text(remote.generated_at)))} ` : "Run Backup Now to publish this computer to Dropbox."}</span>
        <div class="actions left"><button type="button" class="secondary" data-action="activate-profile" ${profile.active ? "disabled" : ""}>Use Profile</button></div>`;
      localComputerList.append(item);
    }
    if (profiles.length === 0) localComputerList.textContent = "No local computers configured";
  }

  if (computerList) {
    computerList.innerHTML = "";
    for (const rawComputer of latestComputers) {
      const computer = rawComputer as ComputerView;
      const machineKey = text(computer.machine_id, text(computer.machine, ""));
      const linkedProfile = latestConfig?.profiles.find((entry) => {
        const profile = entry as ProfileView;
        return text(profile.machine_id, text(profile.id)) === machineKey;
      }) as ProfileView | undefined;
      const folders = Array.isArray(computer.folders) ? computer.folders.length : 0;
      const item = document.createElement("article");
      item.className = "item";
      item.innerHTML = `
        <div class="item-heading">
          <strong>${text(computer.machine_label, machineKey)}</strong>
          <span class="pill ${linkedProfile ? "is-linked" : ""}">${linkedProfile ? "Also local" : "Remote only"}</span>
        </div>
        <span>${folders} folder(s)</span>
        <span>${text(computer.updated_at, text(computer.generated_at))}</span>`;
      computerList.append(item);
    }
    if (latestComputers.length === 0) computerList.textContent = "No remote computers published yet";
  }
}

function renderConfig(config: SafeSyncConfig): void {
  latestConfig = config;
  configLoaded = true;
  if (configPath) configPath.textContent = config.config_path;
  if (profileId) profileId.textContent = config.profile_label || config.profile_id || "-";
  if (machineId) machineId.textContent = config.machine_label || config.machine_id || "-";
  if (remoteBase) remoteBase.textContent = config.remote_base || "-";
  if (settingsForm) {
    for (const [key, value] of Object.entries(config)) {
      const input = settingsForm.elements.namedItem(key) as HTMLInputElement | null;
      if (input && (typeof value === "number" || typeof value === "string")) input.value = String(value);
    }
  }
  if (profileList) {
    profileList.innerHTML = "";
    for (const rawProfile of config.profiles) {
      const profile = rawProfile as ProfileView;
      const item = document.createElement("article");
      item.className = "item profile-card";
      item.dataset.profileId = text(profile.id);
      item.innerHTML = `
        <div class="item-heading">
          <strong>${text(profile.label, text(profile.id))}</strong>
          <span class="pill ${profile.active ? "is-active" : ""}">${profile.active ? "Active" : "Inactive"}</span>
        </div>
        <span>${escapeHtml(text(profile.machine_label, text(profile.machine_id)))}</span>
        <span>${escapeHtml(text(profile.remote_base))}</span>
        <span>${Number(profile.folder_count ?? 0)} folder(s)</span>
        <div class="actions left"><button type="button" class="secondary" data-action="activate-profile" ${profile.active ? "disabled" : ""}>Use Profile</button></div>`;
      profileList.append(item);
    }
    if (config.profiles.length === 0) profileList.textContent = "No local computers configured";
  }
  if (folderList) {
    folderList.innerHTML = "";
    for (const rawFolder of config.folders) {
      const folder = rawFolder as FolderView;
      const item = document.createElement("article");
      item.className = "item folder-editor";
      item.dataset.folderId = text(folder.id);
      const remoteRoot = text(folder.remote_root);
      const remoteLink = dropboxUrl(remoteRoot);
      item.innerHTML = `
        <div class="item-heading">
          <strong>${text(folder.id)}</strong>
          <label class="inline-check"><input type="checkbox" data-folder-field="enabled" ${folder.enabled === false ? "" : "checked"} /> Enabled</label>
        </div>
        <label>Label <input data-folder-field="label" value="${escapeHtml(text(folder.label, text(folder.id)))}" /></label>
        <label>Local path <input data-folder-field="local_path" value="${escapeHtml(text(folder.local_path))}" /></label>
        ${remoteLink
          ? `<a class="dropbox-link" href="${escapeHtml(remoteLink)}" target="_blank" rel="noreferrer">${escapeHtml(remoteLink)}</a>`
          : `<span data-folder-remote-root="${escapeHtml(remoteRoot)}">${escapeHtml(remoteRoot)}</span>`}
        <div class="actions left">
          <button type="button" class="secondary" data-action="save-folder">Save Folder</button>
          <button type="button" class="secondary danger" data-action="remove-folder">Remove Folder</button>
        </div>`;
      folderList.append(item);
    }
    if (config.folders.length === 0) folderList.textContent = "No folders configured";
  }
  renderComputersView();
  renderTransferOptions();
}

async function loadConfig(): Promise<void> {
  setBusy("config");
  try {
    renderConfig(await invoke<SafeSyncConfig>("get_config"));
    setMessage("Settings loaded", "ok");
    holdAction("config");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function saveSettings(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!settingsForm) return;
  setBusy("settings");
  try {
    showUiCommand([
      "config", "update",
      "--machine-label", inputValue(settingsForm, "machine_label"),
      "--profile-label", inputValue(settingsForm, "profile_label"),
      "--remote-base", inputValue(settingsForm, "remote_base"),
      "--poll-interval-seconds", String(numberValue(settingsForm, "poll_interval_seconds")),
      "--debounce-seconds", String(numberValue(settingsForm, "debounce_seconds")),
      "--min-interval-seconds", String(numberValue(settingsForm, "min_interval_seconds")),
      "--fallback-interval-seconds", String(numberValue(settingsForm, "fallback_interval_seconds")),
      "--rate-limit-backoff-seconds", String(numberValue(settingsForm, "rate_limit_backoff_seconds")),
    ]);
    renderConfig(await invoke<SafeSyncConfig>("save_settings", {
      update: {
        machine_label: inputValue(settingsForm, "machine_label"),
        profile_label: inputValue(settingsForm, "profile_label"),
        remote_base: inputValue(settingsForm, "remote_base"),
        poll_interval_seconds: numberValue(settingsForm, "poll_interval_seconds"),
        debounce_seconds: numberValue(settingsForm, "debounce_seconds"),
        min_interval_seconds: numberValue(settingsForm, "min_interval_seconds"),
        fallback_interval_seconds: numberValue(settingsForm, "fallback_interval_seconds"),
        rate_limit_backoff_seconds: numberValue(settingsForm, "rate_limit_backoff_seconds"),
      },
    }));
    setMessage("Settings saved", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function addProfile(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!addProfileForm) return;
  setBusy("profile");
  try {
    const name = inputValue(addProfileForm, "name");
    showUiCommand(["profiles", "add", name]);
    renderConfig(await invoke<SafeSyncConfig>("add_profile", {
      request: {
        name,
      },
    }));
    addProfileForm.reset();
    setMessage("Profile added", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function addFolder(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!addFolderForm) return;
  setBusy("folder");
  try {
    const localPath = inputValue(addFolderForm, "local_path");
    const label = inputValue(addFolderForm, "label");
    const id = label || localPath.split("/").filter(Boolean).pop() || "folder";
    showUiCommand(["folders", "add", id, localPath, "--label", label || id]);
    renderConfig(await invoke<SafeSyncConfig>("add_folder", {
      request: {
        local_path: localPath,
        label,
        remote_path: "",
        trash_path: "",
        disabled: false,
      },
    }));
    addFolderForm.reset();
    setMessage("Folder added", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function pickFolder(): Promise<void> {
  if (!addFolderForm) return;
  setBusy("folder-picker");
  try {
    const selection = await open({
      directory: true,
      multiple: false,
      title: "Choose folder to sync",
    });
    if (typeof selection === "string" && selection.length > 0) {
      const input = addFolderForm.elements.namedItem("local_path") as HTMLInputElement | null;
      if (input) input.value = selection;
      setMessage("Folder selected", "ok");
    }
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function pickTransferDestination(): Promise<void> {
  if (!transferForm) return;
  setBusy("transfer-picker");
  try {
    const selection = await open({ directory: true, multiple: false, title: "Choose transfer destination" });
    if (typeof selection === "string" && selection.length > 0) {
      const input = formField(transferForm, "destination_path") as HTMLInputElement | null;
      if (input) input.value = selection;
      const watchedFolder = formField(transferForm, "destination_folder") as HTMLSelectElement | null;
      if (watchedFolder) watchedFolder.value = "";
      updateTransferCommand();
      setMessage("Transfer destination selected", "ok");
    }
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function openTransferLocal(kind: "source" | "destination"): Promise<void> {
  const path = kind === "source" ? localPathForSource() : transferDestination();
  if (!path) {
    setMessage(kind === "source" ? "This source is not local on the active profile" : "Choose a local destination first", "error");
    return;
  }
  setBusy("open-local");
  try {
    await invoke("open_local_folder", { path });
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function openTransferDropbox(kind: "source" | "destination"): Promise<void> {
  const remoteRoot = kind === "source" ? transferSource : remotePathForDestination();
  if (!remoteRoot) {
    setMessage("This arbitrary local destination has no linked Dropbox folder", "error");
    return;
  }
  setBusy("open-dropbox");
  try {
    await invoke("open_dropbox_location", { request: { remoteRoot } });
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function removeFolder(button: HTMLElement): Promise<void> {
  const item = button.closest<HTMLElement>("[data-folder-id]");
  if (!item) return;
  const id = item.dataset.folderId ?? "";
  setBusy("folder");
  try {
    showUiCommand(["folders", "remove", id]);
    renderConfig(await invoke<SafeSyncConfig>("remove_folder", { request: { id } }));
    setMessage("Folder removed", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function saveFolder(button: HTMLElement): Promise<void> {
  const item = button.closest<HTMLElement>("[data-folder-id]");
  if (!item) return;
  const field = (name: string) => item.querySelector<HTMLInputElement>(`[data-folder-field='${name}']`);
  const id = item.dataset.folderId ?? "";
  const label = field("label")?.value.trim() ?? id;
  const localPath = field("local_path")?.value.trim() ?? "";
  const enabled = field("enabled")?.checked ?? true;
  setBusy("folder");
  try {
    showUiCommand(["folders", "update", id, localPath, "--label", label, enabled ? "--enabled" : "--disabled"]);
    renderConfig(await invoke<SafeSyncConfig>("update_folder", {
      request: {
        id,
        label,
        local_path: localPath,
        enabled,
      },
    }));
    setMessage("Folder saved", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function activateProfile(button: HTMLElement): Promise<void> {
  const item = button.closest<HTMLElement>("[data-profile-id]");
  if (!item) return;
  const id = item.dataset.profileId ?? "";
  setBusy("profile");
  try {
    showUiCommand(["profiles", "activate", id]);
    renderConfig(await invoke<SafeSyncConfig>("activate_profile", { request: { id } }));
    await refreshStatusQuietly();
    setMessage("Profile switched", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function loadComputers(): Promise<void> {
  setBusy("computers");
  try {
    const computers = await invoke<Array<Record<string, unknown>>>("get_computers");
    latestComputers = computers;
    computersLoaded = true;
    renderComputersView();
    renderTransferOptions();
    setMessage("Computers loaded", "ok");
    holdAction("computers");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function listRemote(): Promise<void> {
  if (!transferForm || !transferOutput) return;
  if (!transferSource) {
    transferOutput.textContent = "Choose a published computer and source folder first.";
    setMessage("Choose a source first", "error");
    return;
  }
  setBusy("transfer");
  try {
    showUiCommand(["list", transferSource, "--depth", "2"]);
    const result = await invoke<CommandResult>("list_remote", {
      target: transferSource,
      depth: 2,
    });
    renderRemoteEntries(result.output, transferSource);
    transferOutput.textContent = result.output || "No files found";
    setMessage("Remote source listed", "ok");
    holdAction("transfer");
  } catch (error) {
    transferOutput.textContent = String(error);
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

function renderPreviewList(target: HTMLElement | null, entries: string[], emptyMessage: string, truncated = false): void {
  if (!target) return;
  target.innerHTML = "";
  const lines = entries.length > 0 ? entries : [emptyMessage];
  for (const line of lines) {
    const item = document.createElement("li");
    item.textContent = line;
    target.append(item);
  }
  if (truncated) {
    const item = document.createElement("li");
    item.textContent = "... showing the first 200 entries";
    target.append(item);
  }
}

async function previewTransferContents(): Promise<void> {
  const destination = transferDestination();
  if (!transferSource || !destination) {
    setMessage("Choose a source and destination first", "error");
    return;
  }
  setBusy("transfer-preview");
  try {
    showUiCommand(["list", transferSourceRoot, "--depth", "1"]);
    const [remote, local] = await Promise.all([
      invoke<CommandResult>("list_remote", { target: transferSourceRoot, depth: 1 }),
      invoke<LocalFolderPreview>("list_local_folder", { path: destination }),
    ]);
    if (previewSourcePath) previewSourcePath.textContent = transferSourceRoot;
    if (previewDestinationPath) previewDestinationPath.textContent = local.path;
    renderPreviewList(
      previewSourceList,
      remote.output.split("\n").map((line) => line.trim()).filter(Boolean).slice(0, 200),
      selectedTransferPaths.size > 0 ? `Selected: ${[...selectedTransferPaths].join(", ")}` : "No entries found in the selected remote source.",
      remote.output.split("\n").filter(Boolean).length > 200,
    );
    renderPreviewList(
      previewDestinationList,
      local.entries,
      local.exists ? "This folder is empty." : "This folder will be created by the transfer.",
      local.truncated,
    );
    if (transferPreview) transferPreview.hidden = false;
    setMessage("Source and destination previewed", "ok");
    holdAction("transfer-preview");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function pullRemote(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!transferForm || !transferOutput) return;
  const dryRun = (transferForm.elements.namedItem("dry_run") as HTMLInputElement | null)?.checked ?? true;
  const destination = transferDestination();
  if (!transferSource || !destination) {
    transferOutput.textContent = "Choose a remote source and any local destination folder.";
    setMessage("Choose source and destination", "error");
    return;
  }
  if (cleanSubfolder(selectedValue(transferForm, "destination_subfolder")) === null) {
    transferOutput.textContent = "Destination subfolder cannot contain ..";
    setMessage("Use a safe destination subfolder", "error");
    return;
  }
  setBusy("transfer");
  try {
    showUiCommand(["pull", transferSourceRoot, destination, ...(dryRun ? ["--dry-run"] : []), ...[...selectedTransferPaths].flatMap((path) => ["--select", path])]);
    const result = await invoke<CommandResult>("pull_remote", {
      source: transferSourceRoot,
      destination,
      dryRun,
      selectedPaths: [...selectedTransferPaths],
    });
    transferOutput.textContent = `${result.output || "transfer queued"}\nThe daemon will run it after any active backup. Live progress appears above.`;
    setMessage(dryRun ? "Dry run queued" : "Transfer queued", "ok");
    holdAction("transfer");
  } catch (error) {
    transferOutput.textContent = String(error);
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function copyTransferCommand(): Promise<void> {
  const command = transferCommand?.textContent ?? "";
  if (!command.startsWith("safe-sync pull")) {
    setMessage("Choose a source and destination before copying a command", "error");
    return;
  }
  try {
    await navigator.clipboard.writeText(command);
    setMessage("Command copied", "ok");
    holdAction("copy-transfer-command");
  } catch (error) {
    setMessage(`Could not copy command: ${String(error)}`, "error");
  }
}

async function copyLastCommand(): Promise<void> {
  if (!lastUiCommand) {
    setMessage("No UI command has run yet", "error");
    return;
  }
  try {
    await navigator.clipboard.writeText(lastUiCommand);
    setMessage("Command copied", "ok");
    holdAction("copy-last-command");
  } catch (error) {
    setMessage(`Could not copy command: ${String(error)}`, "error");
  }
}

function openTransferEntry(button: HTMLElement): void {
  const entry = button.dataset.entry;
  if (!entry || !transferSource) return;
  const selected = `${transferSource.replace(/\/+$/, "")}/${entry.replace(/^\/+|\/+$/g, "")}`;
  transferSource = selected;
  transferSourceIsDirectory = true;
  void listRemote();
}

function addTransferEntry(button: HTMLElement): void {
  const entry = button.dataset.entry;
  if (!entry || !transferSource) return;
  const selected = `${transferSource.replace(/\/+$/, "")}/${entry.replace(/^\/+|\/+$/g, "")}`;
  const relative = relativeTransferPath(selected);
  if (!relative) return;
  toggleTransferSelection(button.dataset.directory === "true" ? `${relative}/` : relative);
  setMessage("Transfer selection updated", "ok");
}

function resetTransferSource(): void {
  transferSource = transferSourceRoot;
  transferSourceIsDirectory = true;
  hideTransferBrowser();
  void listRemote();
  setMessage("Browsing selected source folder", "ok");
}

async function refreshStatus(): Promise<void> {
  if (statusRefreshInFlight) return;
  statusRefreshInFlight = true;
  setBusy("refresh");
  try {
    renderStatus(await invoke<SafeSyncStatus>("get_status"));
    holdAction("refresh");
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
    statusRefreshInFlight = false;
  }
}

async function refreshStatusQuietly(): Promise<void> {
  if (statusRefreshInFlight) return;
  statusRefreshInFlight = true;
  try {
    renderStatus(await invoke<SafeSyncStatus>("get_status"));
  } catch (error) {
    renderError(error);
  } finally {
    statusRefreshInFlight = false;
  }
}

function scheduleStatusRefresh(): void {
  if (refreshTimer !== null) window.clearTimeout(refreshTimer);
  const refreshMs = latestStatus && ["syncing", "transferring", "dirty", "cooldown", "backoff"].includes(syncState(latestStatus))
    ? ACTIVE_REFRESH_MS
    : IDLE_REFRESH_MS;
  refreshTimer = window.setTimeout(() => {
    void refreshStatusQuietly();
  }, refreshMs);
}

async function toggleBackend(): Promise<void> {
  if (!latestStatus) await refreshStatus();
  if (latestStatus?.health === "setup_required") {
    if (IS_QUICK_PANEL) {
      await openControlPanel();
    } else {
      activateTab("status");
      setupPanel?.querySelector<HTMLInputElement>("input")?.focus();
    }
    return;
  }
  const action = latestStatus ? desiredAction(latestStatus) : "start";
  setBusy("backend");
  try {
    showUiCommand([action]);
    renderStatus(await invoke<SafeSyncStatus>("control_backend", { action }));
    holdAction("backend");
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
}

async function pickSetupFolder(): Promise<void> {
  if (!setupForm) return;
  setBusy("setup-picker");
  try {
    const selected = await open({ directory: true, multiple: false, title: "Choose a folder to back up" });
    if (typeof selected === "string") {
      const input = setupForm.elements.namedItem("local_path") as HTMLInputElement | null;
      if (input) input.value = selected;
    }
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
}

async function connectDropbox(): Promise<void> {
  setBusy("dropbox-connect");
  try {
    showUiCommand(["connect-dropbox"]);
    const result = await invoke<CommandResult>("connect_dropbox");
    renderDropboxConnection(true);
    setMessage(result.output || "Dropbox connected. Choose a folder to finish setup.", "ok");
    holdAction("dropbox-connect");
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
}

async function completeSetup(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!setupForm) return;
  const folder = inputValue(setupForm, "local_path");
  setBusy("setup");
  try {
    showUiCommand(["setup", "--folder", folder]);
    renderConfig(await invoke<SafeSyncConfig>("complete_setup", { request: { folder } }));
    renderStatus(await invoke<SafeSyncStatus>("get_status"));
    setMessage("Setup complete. Safe Sync is watching your folder.", "ok");
    holdAction("setup");
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
}

async function backupNow(): Promise<void> {
  setBusy("backup");
  try {
    showUiCommand(["backup"]);
    renderStatus(await invoke<SafeSyncStatus>("backup_now"));
    holdAction("backup");
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
}

async function openLogs(): Promise<void> {
  setBusy("logs");
  try {
    showUiCommand(["logs"]);
    await invoke("open_logs");
    holdAction("logs");
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
}

async function openControlPanel(): Promise<void> {
  setBusy("panel");
  try {
    await invoke("open_control_panel");
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
}

async function closeQuickPanel(): Promise<void> {
  await invoke("close_quick_panel");
}

async function quitTray(): Promise<void> {
  await invoke("quit_tray");
}

function activateTab(tab: string): void {
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-tab]")) {
    button.classList.toggle("is-active", button.dataset.tab === tab);
  }
  for (const view of document.querySelectorAll<HTMLElement>("[data-view]")) {
    view.classList.toggle("is-active", view.dataset.view === tab);
  }
  if (tab === "settings" && !configLoaded) void loadConfig();
  if (tab === "computers" && !computersLoaded) void loadComputers();
  if (tab === "transfer") {
    if (!configLoaded) void loadConfig();
    if (!computersLoaded) void loadComputers();
  }
}

window.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.ready = "true";
  document.documentElement.dataset.panel = IS_QUICK_PANEL ? "quick" : "main";
  refreshButton?.addEventListener("click", () => void refreshStatus());
  toggleButton?.addEventListener("click", () => void toggleBackend());
  backupButton?.addEventListener("click", () => void backupNow());
  logsButton?.addEventListener("click", () => void openLogs());
  document.querySelector("[data-action='open-control-panel']")?.addEventListener("click", () => void openControlPanel());
  document.querySelector("[data-action='close-quick']")?.addEventListener("click", () => void closeQuickPanel());
  document.querySelector("[data-action='quit-tray']")?.addEventListener("click", () => void quitTray());
  settingsForm?.addEventListener("submit", (event) => void saveSettings(event));
  addProfileForm?.addEventListener("submit", (event) => void addProfile(event));
  addFolderForm?.addEventListener("submit", (event) => void addFolder(event));
  document.querySelector("[data-action='pick-folder']")?.addEventListener("click", () => void pickFolder());
  document.querySelector("[data-action='pick-setup-folder']")?.addEventListener("click", () => void pickSetupFolder());
  document.querySelector("[data-action='connect-dropbox']")?.addEventListener("click", () => void connectDropbox());
  setupForm?.addEventListener("submit", (event) => void completeSetup(event));
  document.querySelector("[data-action='pick-transfer-destination']")?.addEventListener("click", () => void pickTransferDestination());
  document.querySelector("[data-action='preview-transfer']")?.addEventListener("click", () => void previewTransferContents());
  folderList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "save-folder") void saveFolder(target);
    if (target?.dataset.action === "remove-folder") void removeFolder(target);
  });
  profileList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "activate-profile") void activateProfile(target);
  });
  localComputerList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "activate-profile") void activateProfile(target);
  });
  transferForm?.addEventListener("submit", (event) => void pullRemote(event));
  transferForm?.addEventListener("input", updateTransferCommand);
  transferForm?.addEventListener("change", updateTransferCommand);
  transferEntryList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "open-transfer-entry") openTransferEntry(target);
    if (target?.dataset.action === "toggle-transfer-entry") addTransferEntry(target);
  });
  transferSelectionList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "remove-transfer-entry" && target.dataset.path) toggleTransferSelection(target.dataset.path);
  });
  document.querySelector("[data-action='reload-config']")?.addEventListener("click", () => void loadConfig());
  document.querySelector("[data-action='load-computers']")?.addEventListener("click", () => void loadComputers());
  document.querySelector("[data-action='list-remote']")?.addEventListener("click", () => void listRemote());
  document.querySelector("[data-action='open-source-local']")?.addEventListener("click", () => void openTransferLocal("source"));
  document.querySelector("[data-action='open-source-dropbox']")?.addEventListener("click", () => void openTransferDropbox("source"));
  document.querySelector("[data-action='open-destination-local']")?.addEventListener("click", () => void openTransferLocal("destination"));
  document.querySelector("[data-action='open-destination-dropbox']")?.addEventListener("click", () => void openTransferDropbox("destination"));
  document.querySelector("[data-action='reset-transfer-source']")?.addEventListener("click", resetTransferSource);
  document.querySelector("[data-action='clear-transfer-selection']")?.addEventListener("click", () => {
    selectedTransferPaths.clear();
    renderTransferSelection();
    updateTransferCommand();
  });
  document.querySelector("[data-action='copy-transfer-command']")?.addEventListener("click", () => void copyTransferCommand());
  document.querySelector("[data-action='copy-last-command']")?.addEventListener("click", () => void copyLastCommand());
  document.querySelector("[data-action='refresh-transfer']")?.addEventListener("click", () => {
    void loadConfig();
    void loadComputers();
  });
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-tab]")) {
    button.addEventListener("click", () => activateTab(button.dataset.tab ?? "status"));
  }
  void refreshStatus();
  void loadConfig();
});
