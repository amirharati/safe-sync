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

const AUTO_REFRESH_MS = 10_000;

const stateLabel = document.querySelector<HTMLElement>("[data-status-state]");
const reasonLabel = document.querySelector<HTMLElement>("[data-status-reason]");
const serviceLabel = document.querySelector<HTMLElement>("[data-service-state]");
const syncLabel = document.querySelector<HTMLElement>("[data-sync-state]");
const seenLabel = document.querySelector<HTMLElement>("[data-daemon-seen]");
const logLabel = document.querySelector<HTMLElement>("[data-log-path]");
const refreshLabel = document.querySelector<HTMLElement>("[data-refresh-note]");
const statusDot = document.querySelector<HTMLElement>("[data-status-dot]");
const refreshButton = document.querySelector<HTMLButtonElement>("[data-action='refresh']");
const toggleButton = document.querySelector<HTMLButtonElement>("[data-action='toggle-backend']");
const backupButton = document.querySelector<HTMLButtonElement>("[data-action='backup-now']");
const logsButton = document.querySelector<HTMLButtonElement>("[data-action='open-logs']");

let latestStatus: SafeSyncStatus | null = null;
let busyAction: string | null = null;

function text(value: unknown, fallback = "-"): string {
  if (typeof value === "string" && value.length > 0) {
    return value;
  }
  return fallback;
}

function syncState(status: SafeSyncStatus): string {
  return text(status.sync_state?.state);
}

function tone(status: SafeSyncStatus): string {
  if (status.health === "error") {
    return "error";
  }
  if (status.service_state === "stopped") {
    return "stopped";
  }
  if (status.health === "stale") {
    return "stale";
  }
  if (["syncing", "dirty", "cooldown", "backoff"].includes(syncState(status))) {
    return "active";
  }
  if (status.health === "ok") {
    return "ok";
  }
  return "unknown";
}

function headline(status: SafeSyncStatus): string {
  if (status.health === "error") {
    return "Needs attention";
  }
  if (status.service_state === "stopped") {
    return "Stopped";
  }
  const currentSyncState = syncState(status);
  if (currentSyncState === "syncing") {
    return "Syncing";
  }
  if (currentSyncState === "dirty") {
    return "Changes queued";
  }
  if (currentSyncState === "cooldown") {
    return "Cooling down";
  }
  if (currentSyncState === "backoff") {
    return "Waiting";
  }
  if (status.health === "ok") {
    return "Watching";
  }
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
  if (refreshButton) {
    refreshButton.disabled = isBusy;
  }
  if (toggleButton) {
    toggleButton.disabled = isBusy || latestStatus?.service_state === "unknown";
  }
  if (backupButton) {
    backupButton.disabled = isBusy || latestStatus?.service_state === "unknown";
    backupButton.textContent = action === "backup" ? "Backing Up" : "Backup Now";
  }
  if (logsButton) {
    logsButton.disabled = isBusy || !hasLog(latestStatus);
  }
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
  if (reasonLabel) {
    reasonLabel.textContent = text(status.health_reason);
  }
  if (serviceLabel) {
    serviceLabel.textContent = text(status.service_state);
    serviceLabel.dataset.value = status.service_state;
  }
  if (syncLabel) {
    syncLabel.textContent = syncState(status);
  }
  if (seenLabel) {
    seenLabel.textContent = text(status.daemon_seen_at);
  }
  if (logLabel) {
    logLabel.textContent = text(status.log);
  }
  if (refreshLabel) {
    refreshLabel.textContent = `Auto refresh every ${AUTO_REFRESH_MS / 1000}s`;
  }
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
  if (busyAction) {
    return;
  }
  try {
    renderStatus(await invoke<SafeSyncStatus>("get_status"));
  } catch (error) {
    renderError(error);
  }
}

async function toggleBackend(): Promise<void> {
  if (!latestStatus) {
    await refreshStatus();
  }
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

window.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.ready = "true";
  refreshButton?.addEventListener("click", () => void refreshStatus());
  toggleButton?.addEventListener("click", () => void toggleBackend());
  backupButton?.addEventListener("click", () => void backupNow());
  logsButton?.addEventListener("click", () => void openLogs());
  void refreshStatus();
  window.setInterval(() => void refreshStatusQuietly(), AUTO_REFRESH_MS);
});
