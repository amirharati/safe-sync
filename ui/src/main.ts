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

const stateLabel = document.querySelector<HTMLElement>("[data-status-state]");
const reasonLabel = document.querySelector<HTMLElement>("[data-status-reason]");
const serviceLabel = document.querySelector<HTMLElement>("[data-service-state]");
const syncLabel = document.querySelector<HTMLElement>("[data-sync-state]");
const seenLabel = document.querySelector<HTMLElement>("[data-daemon-seen]");
const logLabel = document.querySelector<HTMLElement>("[data-log-path]");
const refreshButton = document.querySelector<HTMLButtonElement>("[data-action='refresh']");
const startButton = document.querySelector<HTMLButtonElement>("[data-action='start']");
const stopButton = document.querySelector<HTMLButtonElement>("[data-action='stop']");

function text(value: unknown, fallback = "-"): string {
  if (typeof value === "string" && value.length > 0) {
    return value;
  }
  return fallback;
}

function setBusy(isBusy: boolean): void {
  for (const button of [refreshButton, startButton, stopButton]) {
    if (button) {
      button.disabled = isBusy;
    }
  }
}

function renderStatus(status: SafeSyncStatus): void {
  const syncState = status.sync_state ?? {};
  const syncStateName = text(syncState.state);

  if (stateLabel) {
    stateLabel.textContent = status.health === "ok" ? "Healthy" : text(status.health, "Unknown");
    stateLabel.dataset.health = status.health;
  }
  if (reasonLabel) {
    reasonLabel.textContent = text(status.health_reason);
  }
  if (serviceLabel) {
    serviceLabel.textContent = text(status.service_state);
  }
  if (syncLabel) {
    syncLabel.textContent = syncStateName;
  }
  if (seenLabel) {
    seenLabel.textContent = text(status.daemon_seen_at);
  }
  if (logLabel) {
    logLabel.textContent = text(status.log);
  }
}

async function refreshStatus(): Promise<void> {
  setBusy(true);
  try {
    const status = await invoke<SafeSyncStatus>("get_status");
    renderStatus(status);
  } catch (error) {
    renderStatus({
      health: "error",
      health_reason: String(error),
      service_state: "unknown",
      sync_state: {},
      daemon_seen_at: null,
      log: null,
    });
  } finally {
    setBusy(false);
  }
}

async function controlBackend(action: "start" | "stop"): Promise<void> {
  setBusy(true);
  try {
    const status = await invoke<SafeSyncStatus>("control_backend", { action });
    renderStatus(status);
  } catch (error) {
    renderStatus({
      health: "error",
      health_reason: String(error),
      service_state: "unknown",
      sync_state: {},
      daemon_seen_at: null,
      log: null,
    });
  } finally {
    setBusy(false);
  }
}

window.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.ready = "true";
  refreshButton?.addEventListener("click", () => void refreshStatus());
  startButton?.addEventListener("click", () => void controlBackend("start"));
  stopButton?.addEventListener("click", () => void controlBackend("stop"));
  void refreshStatus();
});
