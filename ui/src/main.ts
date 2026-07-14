import { invoke } from "@tauri-apps/api/core";
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
  machine_id: string | null;
  machine_label: string | null;
  remote_base: string | null;
  poll_interval_seconds: number;
  debounce_seconds: number;
  min_interval_seconds: number;
  fallback_interval_seconds: number;
  rate_limit_backoff_seconds: number;
  folders: Array<Record<string, unknown>>;
};

type CommandResult = { ok: boolean; output: string };

const AUTO_REFRESH_MS = 10_000;

const stateLabel = document.querySelector<HTMLElement>("[data-status-state]");
const reasonLabel = document.querySelector<HTMLElement>("[data-status-reason]");
const serviceLabel = document.querySelector<HTMLElement>("[data-service-state]");
const syncLabel = document.querySelector<HTMLElement>("[data-sync-state]");
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
const machineId = document.querySelector<HTMLElement>("[data-machine-id]");
const remoteBase = document.querySelector<HTMLElement>("[data-remote-base]");
const settingsForm = document.querySelector<HTMLFormElement>("[data-settings-form]");
const addFolderForm = document.querySelector<HTMLFormElement>("[data-add-folder-form]");
const folderList = document.querySelector<HTMLElement>("[data-folder-list]");
const computerList = document.querySelector<HTMLElement>("[data-computer-list]");
const transferForm = document.querySelector<HTMLFormElement>("[data-transfer-form]");
const transferOutput = document.querySelector<HTMLElement>("[data-transfer-output]");

let latestStatus: SafeSyncStatus | null = null;
let busyAction: string | null = null;
let configLoaded = false;
let computersLoaded = false;

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
  if (status.service_state === "stopped") return "stopped";
  if (status.health === "stale") return "stale";
  if (["syncing", "dirty", "cooldown", "backoff"].includes(syncState(status))) return "active";
  if (status.health === "ok") return "ok";
  return "unknown";
}

function headline(status: SafeSyncStatus): string {
  if (status.health === "error") return "Needs attention";
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

function hasLog(status: SafeSyncStatus | null): boolean {
  return Boolean(status?.log && status.log.length > 0);
}

function setBusy(action: string | null): void {
  busyAction = action;
  const isBusy = action !== null;
  for (const button of document.querySelectorAll<HTMLButtonElement>("button")) {
    button.disabled = isBusy && button.dataset.action !== "refresh";
  }
  if (refreshButton) refreshButton.disabled = isBusy;
  if (toggleButton) toggleButton.disabled = isBusy || latestStatus?.service_state === "unknown";
  if (backupButton) {
    backupButton.disabled = isBusy || latestStatus?.service_state === "unknown";
    backupButton.textContent = action === "backup" ? "Backing Up" : "Backup Now";
  }
  if (logsButton) logsButton.disabled = isBusy || !hasLog(latestStatus);
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
  if (seenLabel) seenLabel.textContent = text(status.daemon_seen_at);
  if (logLabel) logLabel.textContent = text(status.log);
  if (refreshLabel) refreshLabel.textContent = `Auto refresh every ${AUTO_REFRESH_MS / 1000}s`;
  if (toggleButton) {
    toggleButton.textContent = action === "stop" ? "Stop Backend" : "Start Backend";
    toggleButton.dataset.intent = action;
  }
  setBusy(busyAction);
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

function numberValue(form: HTMLFormElement, name: string): number {
  return Number(inputValue(form, name));
}

function renderConfig(config: SafeSyncConfig): void {
  configLoaded = true;
  if (configPath) configPath.textContent = config.config_path;
  if (machineId) machineId.textContent = config.machine_label || config.machine_id || "-";
  if (remoteBase) remoteBase.textContent = config.remote_base || "-";
  if (settingsForm) {
    for (const [key, value] of Object.entries(config)) {
      const input = settingsForm.elements.namedItem(key) as HTMLInputElement | null;
      if (input && typeof value === "number") input.value = String(value);
    }
  }
  if (folderList) {
    folderList.innerHTML = "";
    for (const folder of config.folders) {
      const item = document.createElement("article");
      item.className = "item";
      item.innerHTML = `<strong>${text(folder.label, text(folder.id))}</strong><span>${text(folder.local_path)}</span><span>${text(folder.remote_root)}</span>`;
      folderList.append(item);
    }
    if (config.folders.length === 0) folderList.textContent = "No folders configured";
  }
}

async function loadConfig(): Promise<void> {
  setBusy("config");
  try {
    renderConfig(await invoke<SafeSyncConfig>("get_config"));
    setMessage("Settings loaded", "ok");
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

async function addFolder(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!addFolderForm) return;
  setBusy("folder");
  try {
    renderConfig(await invoke<SafeSyncConfig>("add_folder", {
      request: {
        id: inputValue(addFolderForm, "id"),
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

async function loadComputers(): Promise<void> {
  setBusy("computers");
  try {
    const computers = await invoke<Array<Record<string, unknown>>>("get_computers");
    computersLoaded = true;
    if (computerList) {
      computerList.innerHTML = "";
      for (const computer of computers) {
        const item = document.createElement("article");
        item.className = "item";
        const folders = Array.isArray(computer.folders) ? computer.folders.length : 0;
        item.innerHTML = `<strong>${text(computer.machine_label, text(computer.machine_id, text(computer.machine)))}</strong><span>${folders} folder(s)</span><span>${text(computer.updated_at, text(computer.generated_at))}</span>`;
        computerList.append(item);
      }
      if (computers.length === 0) computerList.textContent = "No computers found";
    }
    setMessage("Computers loaded", "ok");
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
  } catch (error) {
    transferOutput.textContent = String(error);
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function refreshStatus(): Promise<void> {
  setBusy("refresh");
  try {
    renderStatus(await invoke<SafeSyncStatus>("get_status"));
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
}

async function refreshStatusQuietly(): Promise<void> {
  if (busyAction) return;
  try {
    renderStatus(await invoke<SafeSyncStatus>("get_status"));
  } catch (error) {
    renderError(error);
  }
}

async function toggleBackend(): Promise<void> {
  if (!latestStatus) await refreshStatus();
  const action = latestStatus ? desiredAction(latestStatus) : "start";
  setBusy(action);
  try {
    renderStatus(await invoke<SafeSyncStatus>("control_backend", { action }));
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
  } catch (error) {
    renderError(error);
  } finally {
    setBusy(null);
  }
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
  refreshButton?.addEventListener("click", () => void refreshStatus());
  toggleButton?.addEventListener("click", () => void toggleBackend());
  backupButton?.addEventListener("click", () => void backupNow());
  logsButton?.addEventListener("click", () => void openLogs());
  settingsForm?.addEventListener("submit", (event) => void saveSettings(event));
  addFolderForm?.addEventListener("submit", (event) => void addFolder(event));
  transferForm?.addEventListener("submit", (event) => void pullRemote(event));
  document.querySelector("[data-action='reload-config']")?.addEventListener("click", () => void loadConfig());
  document.querySelector("[data-action='load-computers']")?.addEventListener("click", () => void loadComputers());
  document.querySelector("[data-action='list-remote']")?.addEventListener("click", () => void listRemote());
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-tab]")) {
    button.addEventListener("click", () => activateTab(button.dataset.tab ?? "status"));
  }
  void refreshStatus();
  void loadConfig();
  window.setInterval(() => void refreshStatusQuietly(), AUTO_REFRESH_MS);
});
