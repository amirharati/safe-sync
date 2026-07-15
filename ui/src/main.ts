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
  poll_interval_seconds: number;
  debounce_seconds: number;
  min_interval_seconds: number;
  fallback_interval_seconds: number;
  rate_limit_backoff_seconds: number;
  folders: Array<Record<string, unknown>>;
  profiles: Array<Record<string, unknown>>;
};

type CommandResult = { ok: boolean; output: string };

type FolderView = Record<string, unknown> & { id?: string; label?: string; local_path?: string; enabled?: boolean };
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

function text(value: unknown, fallback = "-"): string {
  return typeof value === "string" && value.length > 0 ? value : fallback;
}

function setMessage(value: string, tone = "neutral"): void {
  if (message) {
    message.textContent = value;
    message.dataset.tone = tone;
  }
}

function syncState(status: SafeSyncStatus): string {
  return text(status.sync_state?.state);
}

function tone(status: SafeSyncStatus): string {
  if (status.health === "error") return "error";
  if (status.health === "warning") return "warning";
  if (status.service_state === "stopped") return "stopped";
  if (status.health === "stale") return "stale";
  if (["syncing", "dirty", "cooldown", "backoff"].includes(syncState(status))) return "active";
  if (status.health === "ok") return "ok";
  return "unknown";
}

function headline(status: SafeSyncStatus): string {
  if (status.health === "error") return "Needs attention";
  if (status.health === "warning") return syncState(status) === "backoff" ? "Waiting" : "Warning";
  if (status.service_state === "stopped") return "Stopped";
  const currentSyncState = syncState(status);
  if (currentSyncState === "syncing") return "Syncing";
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
  if (syncStateValue === "syncing" || syncStateValue === "backoff" || syncStateValue === "cooldown") {
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
  if (syncState(status) === "syncing") return "Waiting for file detail";
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
  if (action === "reload-config") return "config";
  if (action === "pick-folder") return "folder-picker";
  if (action === "open-dropbox") return "dropbox";
  if (action === "activate-profile") return "profile";
  if (action === "remove-folder") return "folder";
  if (action === "load-computers") return "computers";
  if (action === "list-remote") return "transfer";
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
    backupButton.disabled = action === "backup" || isHeld("backup") || latestStatus?.service_state !== "running";
    backupButton.textContent = action === "backup" ? "Backing Up" : "Backup Now";
    backupButton.title = latestStatus?.service_state === "running" ? "" : "Start the backend before running Backup Now";
  }
  if (logsButton) logsButton.disabled = action === "logs" || isHeld("logs") || !hasLog(latestStatus);
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
  const refreshMs = ["syncing", "dirty", "cooldown", "backoff"].includes(syncState(status))
    ? ACTIVE_REFRESH_MS
    : IDLE_REFRESH_MS;
  if (refreshLabel) refreshLabel.textContent = `Auto refresh every ${refreshMs / 1000}s`;
  if (toggleButton) {
    toggleButton.textContent = action === "stop" ? "Stop Backend" : "Start Backend";
    toggleButton.dataset.intent = action;
  }
  setBusy(busyAction);
  scheduleStatusRefresh();
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
        <span>${remote ? `Registry: ${escapeHtml(text(remote.updated_at, text(remote.generated_at)))} ` : "Run Backup Now to publish this computer to Dropbox."}</span>`;
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
    renderConfig(await invoke<SafeSyncConfig>("add_folder", {
      request: {
        local_path: inputValue(addFolderForm, "local_path"),
        label: inputValue(addFolderForm, "label"),
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

async function removeFolder(button: HTMLElement): Promise<void> {
  const item = button.closest<HTMLElement>("[data-folder-id]");
  if (!item) return;
  const id = item.dataset.folderId ?? "";
  setBusy("folder");
  try {
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
  setBusy("folder");
  try {
    renderConfig(await invoke<SafeSyncConfig>("update_folder", {
      request: {
        id,
        label: field("label")?.value.trim() ?? id,
        local_path: field("local_path")?.value.trim() ?? "",
        enabled: field("enabled")?.checked ?? true,
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
  setBusy("transfer");
  try {
    const result = await invoke<CommandResult>("list_remote", {
      target: inputValue(transferForm, "source"),
      depth: numberValue(transferForm, "depth") || 2,
    });
    transferOutput.textContent = result.output || "No output";
    setMessage("Remote listed", "ok");
    holdAction("transfer");
  } catch (error) {
    transferOutput.textContent = String(error);
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function pullRemote(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!transferForm || !transferOutput) return;
  const dryRun = (transferForm.elements.namedItem("dry_run") as HTMLInputElement | null)?.checked ?? true;
  setBusy("transfer");
  try {
    const result = await invoke<CommandResult>("pull_remote", {
      source: inputValue(transferForm, "source"),
      destination: inputValue(transferForm, "destination"),
      dryRun,
    });
    transferOutput.textContent = result.output || "Done";
    setMessage(dryRun ? "Dry run complete" : "Pull complete", "ok");
    holdAction("transfer");
  } catch (error) {
    transferOutput.textContent = String(error);
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
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
  const refreshMs = latestStatus && ["syncing", "dirty", "cooldown", "backoff"].includes(syncState(latestStatus))
    ? ACTIVE_REFRESH_MS
    : IDLE_REFRESH_MS;
  refreshTimer = window.setTimeout(() => {
    void refreshStatusQuietly();
  }, refreshMs);
}

async function toggleBackend(): Promise<void> {
  if (!latestStatus) await refreshStatus();
  const action = latestStatus ? desiredAction(latestStatus) : "start";
  setBusy("backend");
  try {
    renderStatus(await invoke<SafeSyncStatus>("control_backend", { action }));
    holdAction("backend");
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
}

async function backupNow(): Promise<void> {
  setBusy("backup");
  try {
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
  folderList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "save-folder") void saveFolder(target);
    if (target?.dataset.action === "remove-folder") void removeFolder(target);
  });
  profileList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "activate-profile") void activateProfile(target);
  });
  transferForm?.addEventListener("submit", (event) => void pullRemote(event));
  document.querySelector("[data-action='reload-config']")?.addEventListener("click", () => void loadConfig());
  document.querySelector("[data-action='load-computers']")?.addEventListener("click", () => void loadComputers());
  document.querySelector("[data-action='list-remote']")?.addEventListener("click", () => void listRemote());
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-tab]")) {
    button.addEventListener("click", () => activateTab(button.dataset.tab ?? "status"));
  }
  void refreshStatus();
  void loadConfig();
});
