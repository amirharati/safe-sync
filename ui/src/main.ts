import { invoke } from "@tauri-apps/api/core";
import { open } from "@tauri-apps/plugin-dialog";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { renderUserGuide } from "./help";
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
  logging: Record<string, unknown>;
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

type AuditStatus = {
  health: string;
  level: string;
  used_local_bytes: number;
  max_local_bytes: number;
  pending_cloud_segments: number;
  gaps: Array<Record<string, unknown>>;
  history_complete?: boolean;
  history_gap_count?: number;
  replication: Record<string, unknown>;
};

type AuditEvent = {
  event_id: string;
  sequence: number;
  occurred_at: string;
  severity: string;
  event_type: string;
  component: string;
  correlation: Record<string, unknown>;
  data: Record<string, unknown>;
};

const IDLE_REFRESH_MS = 10_000;
const ACTIVE_REFRESH_MS = 1_500;
const ACTION_FEEDBACK_MS = 1800;
const IS_QUICK_PANEL = getCurrentWindow().label === "quick";

const stateLabel = document.querySelector<HTMLElement>("[data-status-state]");
const reasonLabel = document.querySelector<HTMLElement>("[data-status-reason]");
const serviceLabel = document.querySelector<HTMLElement>("[data-service-state]");
const syncLabel = document.querySelector<HTMLElement>("[data-sync-state]");
const configuredFoldersLabel = document.querySelector<HTMLElement>("[data-configured-folders]");
const overallProgressLabel = document.querySelector<HTMLElement>("[data-overall-progress]");
const currentFolderHeading = document.querySelector<HTMLElement>("[data-current-folder-heading]");
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
const reconnectDropboxButton = document.querySelector<HTMLButtonElement>("[data-action='reconnect-dropbox']");
const helpGuide = document.querySelector<HTMLElement>("[data-help-guide]");
const jobList = document.querySelector<HTMLElement>("[data-job-list]");
const jobOutput = document.querySelector<HTMLElement>("[data-job-output]");
const linkList = document.querySelector<HTMLElement>("[data-link-list]");
const addLinkForm = document.querySelector<HTMLFormElement>("[data-add-link-form]");
const historyFolder = document.querySelector<HTMLSelectElement>("[data-history-folder]");
const recoveryState = document.querySelector<HTMLElement>("[data-recovery-state]");
const recoveryGuidance = document.querySelector<HTMLElement>("[data-recovery-guidance]");
const recoveryLegacyActions = document.querySelector<HTMLElement>("[data-recovery-legacy-actions]");
const recoveryClearLegacyButton = document.querySelector<HTMLButtonElement>("[data-action='clear-legacy-recovery']");
const recoveryEnterButton = document.querySelector<HTMLButtonElement>("[data-action='enter-recovery']");
const recoveryRewoundButton = document.querySelector<HTMLButtonElement>("[data-action='mark-recovery-rewound']");
const recoveryExportButton = document.querySelector<HTMLButtonElement>("[data-action='export-recovery']");
const recoveryOpenExportButton = document.querySelector<HTMLButtonElement>("[data-action='open-recovery-export']");
const recoveryUndoButton = document.querySelector<HTMLButtonElement>("[data-action='mark-recovery-undo']");
const recoveryVerifyButton = document.querySelector<HTMLButtonElement>("[data-action='verify-recovery']");
const recoveryExitButton = document.querySelector<HTMLButtonElement>("[data-action='exit-recovery']");
const restoreLocal = document.querySelector<HTMLElement>("[data-restore-local]");
const restoreRemote = document.querySelector<HTMLElement>("[data-restore-remote]");
const restoreDestination = document.querySelector<HTMLElement>("[data-restore-destination]");
const recoveryVerification = document.querySelector<HTMLElement>("[data-recovery-verification]");
const recoveryOperationModal = document.querySelector<HTMLElement>("[data-recovery-operation-modal]");
const recoveryOperationTitle = document.querySelector<HTMLElement>("[data-recovery-operation-title]");
const recoveryOperationDetail = document.querySelector<HTMLElement>("[data-recovery-operation-detail]");
const recoveryStatusNotice = document.querySelector<HTMLElement>("[data-recovery-status-notice]");
const recoveryStatusSummary = document.querySelector<HTMLElement>("[data-recovery-status-summary]");
const recoveryEntryProgress = document.querySelectorAll<HTMLElement>("[data-recovery-entry-progress]");
const recoveryEntryProgressText = document.querySelectorAll<HTMLElement>("[data-recovery-entry-progress-text]");
const recoveryCancelActions = document.querySelector<HTMLElement>("[data-recovery-cancel-actions]");
const cancelRemoteCopyFact = document.querySelector<HTMLElement>("[data-cancel-remote-copy-fact]");
const cancelRemoteCopyDestination = document.querySelector<HTMLElement>("[data-cancel-remote-copy-destination]");
const recoveryDownloadList = document.querySelector<HTMLElement>("[data-recovery-download-list]");
const recoveryDownloadSort = document.querySelector<HTMLSelectElement>("[data-recovery-download-sort]");
const recoveryRemoveAllButton = document.querySelector<HTMLButtonElement>("[data-action='remove-all-recovery-downloads']");
const activityFilterForm = document.querySelector<HTMLFormElement>("[data-activity-filter-form]");
const logLevelForm = document.querySelector<HTMLFormElement>("[data-log-level-form]");
const auditEvents = document.querySelector<HTMLElement>("[data-audit-events]");
const auditHealth = document.querySelector<HTMLElement>("[data-audit-health]");
const auditLevel = document.querySelector<HTMLElement>("[data-audit-level]");
const auditUsage = document.querySelector<HTMLElement>("[data-audit-usage]");
const auditPending = document.querySelector<HTMLElement>("[data-audit-pending]");
const auditCloudTime = document.querySelector<HTMLElement>("[data-audit-cloud-time]");
const auditGaps = document.querySelector<HTMLElement>("[data-audit-gaps]");

let latestStatus: SafeSyncStatus | null = null;
let latestRecovery: Record<string, unknown> | null = null;
let latestRecoveryDownloads: Array<Record<string, unknown>> = [];
let busyAction: string | null = null;
let feedbackAction: string | null = null;
let feedbackTimer: number | null = null;
let refreshTimer: number | null = null;
let statusRefreshInFlight = false;
let recoveryActionInFlight = false;
let recoveryOperationTrigger: HTMLElement | null = null;
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
let jobsLoaded = false;
let linksLoaded = false;
let activityLoaded = false;
let latestJobs: Array<Record<string, unknown>> = [];

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
  if (["syncing", "transferring", "dirty", "cooldown", "backoff", "recovery_paused"].includes(syncState(status))) return "active";
  if (status.health === "ok") return "ok";
  return "unknown";
}

function headline(status: SafeSyncStatus): string {
  if (status.health === "setup_required") return "Setup required";
  if (status.health_reason.includes("Dropbox authorization is invalid or revoked")) return "Reconnect Dropbox";
  if (status.health === "error") return "Needs attention";
  if (status.health === "warning") return syncState(status) === "backoff" ? "Waiting" : "Warning";
  if (status.service_state === "stopped") return "Stopped";
  if (status.sync_state?.recovery_resume_pending === true) return "Preparing backup";
  const currentSyncState = syncState(status);
  if (currentSyncState === "syncing") return "Syncing";
  if (currentSyncState === "transferring") return "Transferring";
  if (currentSyncState === "dirty") return "Changes queued";
  if (currentSyncState === "cooldown") return "Cooling down";
  if (currentSyncState === "backoff") return "Waiting";
  if (currentSyncState === "recovery_paused") return "Recovery Mode locked";
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
  if (["syncing", "publishing", "retry_pending", "backoff", "cooldown"].includes(syncStateValue) && index > 0 && total > 0) {
    return `${folderLabel} (${index}/${total})`;
  }
  return folderLabel;
}

function configuredFoldersSummary(status: SafeSyncStatus): string {
  const rawFolders = status.sync_state?.folders;
  const folders = Array.isArray(rawFolders) ? rawFolders : [];
  const names = folders
    .map((raw) => {
      if (!raw || typeof raw !== "object") return "";
      const folder = raw as Record<string, unknown>;
      return text(folder.label, text(folder.id, ""));
    })
    .filter((name) => name.length > 0);
  const configuredCount = Number(status.sync_state?.configured_folder_count ?? folders.length);
  if (names.length > 0) return `${configuredCount}: ${names.join(", ")}`;
  if (configuredCount > 0) return String(configuredCount);
  return "None";
}

function overallBackupSummary(status: SafeSyncStatus): string {
  const rawFolders = status.sync_state?.folders;
  const folders = Array.isArray(rawFolders) ? rawFolders : [];
  const scope = text(status.sync_state?.backup_scope, "full");
  const rawScheduled = status.sync_state?.scheduled_folders;
  const scheduled = Array.isArray(rawScheduled) ? new Set(rawScheduled.map(String)) : null;
  const targeted = scope === "targeted" && scheduled !== null;
  const total = targeted
    ? scheduled.size
    : Number(status.sync_state?.configured_folder_count ?? folders.length);
  if (total <= 0) return "No folders configured";

  const rawPending = status.sync_state?.pending_folders;
  const rawCompleted = status.sync_state?.completed_folders;
  const pendingIds = Array.isArray(rawPending) ? new Set(rawPending.map(String)) : null;
  const completedIds = Array.isArray(rawCompleted) ? new Set(rawCompleted.map(String)) : null;
  const pending = pendingIds === null
    ? null
    : Math.min(total, targeted ? [...pendingIds].filter((id) => scheduled?.has(id)).length : pendingIds.size);
  const explicitlyCompleted = completedIds === null
    ? null
    : Math.min(total, targeted ? [...completedIds].filter((id) => scheduled?.has(id)).length : completedIds.size);
  if (pending === null && explicitlyCompleted === null) return "Backup cycle not started";

  const completed = pending === null ? explicitlyCompleted ?? 0 : Math.max(0, total - pending);
  const state = syncState(status);
  const active = pending !== null && pending > 0 && ["syncing", "publishing"].includes(state) ? 1 : 0;
  const waiting = pending === null ? Math.max(0, total - completed - active) : Math.max(0, pending - active);
  const folderKind = targeted ? "changed folders" : "folders";
  const parts = [`${completed.toLocaleString()}/${total.toLocaleString()} ${folderKind} complete`];
  if (active > 0) parts.push(`${active} active`);
  if (waiting > 0) parts.push(`${waiting.toLocaleString()} waiting`);
  return parts.join(" · ");
}

function currentFolderHeadingText(status: SafeSyncStatus): string {
  const state = syncState(status);
  if (state === "backoff" || state === "retry_pending") return "Pending folder";
  if (state === "syncing" || state === "publishing") return "Current folder";
  return "Last folder";
}

function progressSummary(status: SafeSyncStatus): string {
  const syncStateValue = syncState(status);
  const phase = text(status.sync_state?.sync_phase, "");
  if (["syncing", "publishing"].includes(syncStateValue) && phase === "scanning") {
    const checked = Number(status.sync_state?.checks_completed ?? 0);
    const listed = Number(status.sync_state?.listed_entries ?? 0);
    if (checked > 0 && listed > 0) return `Scanning and comparing · ${checked.toLocaleString()} checked · ${listed.toLocaleString()} listed`;
    if (checked > 0) return `Scanning and comparing · ${checked.toLocaleString()} checked`;
    if (listed > 0) return `Scanning and comparing · ${listed.toLocaleString()} listed`;
    return "Scanning and comparing";
  }
  if (["syncing", "publishing"].includes(syncStateValue) && phase === "transferring") {
    const percent = Number(status.sync_state?.progress_percent ?? 0);
    const transferred = Number(status.sync_state?.transferred_files ?? 0);
    const total = Number(status.sync_state?.total_transfer_files ?? 0);
    const bytesDone = text(status.sync_state?.transferred_bytes_display, "");
    const bytesTotal = text(status.sync_state?.total_bytes_display, "");
    const failed = Number(status.sync_state?.failed_transfer_files ?? 0);
    const eta = text(status.sync_state?.eta, "");
    if (total <= 0) return "Transferring — preparing stable totals";
    const parts = [`Transferring — ${percent}%`, `${transferred.toLocaleString()}/${total.toLocaleString()} files`];
    if (bytesDone && bytesTotal) parts.push(`${bytesDone}/${bytesTotal}`);
    if (failed > 0) parts.push(`${failed.toLocaleString()} failed this attempt`);
    if (eta) parts.push(`ETA ${eta}`);
    return parts.join(" · ");
  }
  if (["syncing", "publishing"].includes(syncStateValue) && phase === "finalizing") return "Finalizing folder backup";
  const backoff = Number(status.sync_state?.backoff_remaining_seconds ?? 0);
  if (syncStateValue === "backoff" && backoff > 0) return `Retrying in ${Math.ceil(backoff)}s`;
  const cooldown = Number(status.sync_state?.cooldown_remaining_seconds ?? 0);
  if (syncStateValue === "cooldown" && cooldown > 0) return `Waiting ${Math.ceil(cooldown)}s before next sync`;
  const live = text(status.sync_state?.last_progress, "");
  if (live) return live;
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
  if (action === "reconnect-dropbox") return "dropbox-connect";
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
  if (["load-jobs", "show-job", "open-job-staging", "open-job-destination", "apply-job", "reconcile-job", "rollback-job"].includes(action ?? "")) return "jobs";
  if (["load-link-status", "review-link", "remove-link", "add-link"].includes(action ?? "")) return "links";
  if (["load-history", "recover-history"].includes(action ?? "")) return "history";
  if (["load-activity", "filter-activity", "show-recent-warnings", "set-log-level", "debug-two-hours", "sync-audit-logs"].includes(action ?? "")) return "activity";
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
    if (button.hasAttribute("data-recovery-control")) continue;
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

function showRecoveryOperation(action: RecoveryAction | "remove-downloads"): void {
  const copy: Record<RecoveryAction | "remove-downloads", [string, string]> = {
    enter: ["Entering Recovery Mode", "Waiting for any current folder operation to finish, then locking every outbound backup."],
    "clear-legacy": ["Clearing the Old Pause", "Waiting for the backup safety barrier, updating the backend, and resuming queued work."],
    "mark-rewound": ["Recording Dropbox Rewind", "Saving the completed Rewind step while every outbound backup remains locked."],
    export: ["Creating the Historical Copy", "Downloading and verifying the rewound Dropbox folder. This can take time; do not change Dropbox."],
    "mark-undo-complete": ["Recording Undo-Rewind", "Saving the completed Dropbox step while backups remain locked."],
    verify: ["Verifying Current State", "Comparing Dropbox with the unchanged local folder. The lock remains active until they match."],
    exit: ["Exiting Recovery Mode", "Running the final safety verification before queued backups are allowed to resume."],
    cancel: ["Cancelling Recovery Safely", "Checking Dropbox against local. If they differ, Safe Sync will restore Dropbox from the current local folder, verify equality, and then resume backups."],
    "save-remote-copy": ["Saving the Dropbox Copy", "Downloading the current Dropbox folder into isolated local storage and verifying every path and content hash. Recovery Mode remains locked."],
    "remove-downloads": ["Deleting Local Recovery Copies", "Removing only the selected downloaded recovery folders and their catalog records. Watched folders and Dropbox are never changed."],
  };
  if (recoveryOperationTitle) recoveryOperationTitle.textContent = copy[action][0];
  if (recoveryOperationDetail) recoveryOperationDetail.textContent = copy[action][1];
  recoveryOperationTrigger = document.activeElement instanceof HTMLElement ? document.activeElement : null;
  if (recoveryOperationModal) recoveryOperationModal.hidden = false;
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-recovery-control]")) button.disabled = true;
  document.querySelector<HTMLElement>("main.shell")?.setAttribute("inert", "");
  document.body.dataset.operationBusy = "true";
  recoveryOperationModal?.focus();
}

function hideRecoveryOperation(): void {
  if (recoveryOperationModal) recoveryOperationModal.hidden = true;
  document.querySelector<HTMLElement>("main.shell")?.removeAttribute("inert");
  delete document.body.dataset.operationBusy;
  if (latestRecovery) updateRestoreActions(latestRecovery);
  if (recoveryOperationTrigger && !recoveryOperationTrigger.hidden && !(recoveryOperationTrigger instanceof HTMLButtonElement && recoveryOperationTrigger.disabled)) {
    recoveryOperationTrigger.focus();
  }
  recoveryOperationTrigger = null;
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
    reasonLabel.textContent = status.sync_state?.recovery_resume_pending === true
      ? text(status.sync_state?.note, "Recovery complete; preparing normal backup")
      : text(status.health_reason);
  }
  if (reconnectDropboxButton) reconnectDropboxButton.hidden = !status.health_reason.includes("Dropbox authorization is invalid or revoked");
  if (setupPanel) setupPanel.hidden = status.health !== "setup_required";
  if (status.health === "setup_required" && !dropboxConnectionKnown) void refreshDropboxConnection();
  if (serviceLabel) {
    serviceLabel.textContent = text(status.service_state);
    serviceLabel.dataset.value = status.service_state;
  }
  if (syncLabel) syncLabel.textContent = status.sync_state?.recovery_resume_pending === true ? "resuming" : syncState(status);
  if (configuredFoldersLabel) configuredFoldersLabel.textContent = configuredFoldersSummary(status);
  if (overallProgressLabel) overallProgressLabel.textContent = overallBackupSummary(status);
  if (currentFolderHeading) currentFolderHeading.textContent = currentFolderHeadingText(status);
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
  const clone = (transferForm.elements.namedItem("clone") as HTMLInputElement | null)?.checked ?? false;
  const runButton = document.querySelector<HTMLButtonElement>("[data-action='run-transfer']");
  if (runButton) runButton.textContent = dryRun ? "Compare" : "Stage Receive Job";
  if (subfolder === null) {
    transferCommand.textContent = "Destination subfolder cannot contain ..";
  } else if (!transferSource || !destination) {
    transferCommand.textContent = "Choose a source and destination folder.";
  } else {
    const selected = [...selectedTransferPaths].map((path) => ` --select ${shellQuote(path)}`).join("");
    const command = clone ? "receive" : "pull";
    transferCommand.textContent = `safe-sync ${command} ${shellQuote(transferSourceRoot)} ${shellQuote(destination)}${clone ? " --clone" : ""}${dryRun ? " --dry-run" : ""}${selected}`;
  }
  updateTransferLocationActions();
}

function renderLinkAndHistoryOptions(): void {
  if (historyFolder) {
    const prior = historyFolder.value;
    historyFolder.innerHTML = "";
    for (const raw of latestConfig?.folders ?? []) {
      const folder = raw as FolderView;
      if (!folder.id) continue;
      const option = document.createElement("option");
      option.value = folder.id;
      option.textContent = text(folder.label, folder.id);
      historyFolder.append(option);
    }
    if (prior && [...historyFolder.options].some((option) => option.value === prior)) historyFolder.value = prior;
    renderRestoreFolder();
  }
  if (!addLinkForm) return;
  const local = formField(addLinkForm, "local_folder") as HTMLSelectElement | null;
  const peer = formField(addLinkForm, "peer_machine") as HTMLSelectElement | null;
  const peerFolder = formField(addLinkForm, "peer_folder") as HTMLSelectElement | null;
  if (!local || !peer || !peerFolder) return;
  const priorLocal = local.value;
  const priorPeer = peer.value;
  local.innerHTML = "";
  for (const raw of latestConfig?.folders ?? []) {
    const folder = raw as FolderView;
    if (!folder.id) continue;
    const option = document.createElement("option");
    option.value = folder.id;
    option.textContent = text(folder.label, folder.id);
    local.append(option);
  }
  if (priorLocal && [...local.options].some((option) => option.value === priorLocal)) local.value = priorLocal;
  peer.innerHTML = "";
  for (const raw of latestComputers) {
    const computer = raw as ComputerView;
    const id = text(computer.machine_id, "");
    if (!id || id === latestConfig?.machine_id) continue;
    const option = document.createElement("option");
    option.value = id;
    option.textContent = text(computer.machine_label, id);
    peer.append(option);
  }
  if (priorPeer && [...peer.options].some((option) => option.value === priorPeer)) peer.value = priorPeer;
  const renderPeerFolders = (): void => {
    peerFolder.innerHTML = "";
    const computer = remoteComputerByMachineId(peer.value);
    for (const raw of Array.isArray(computer?.folders) ? computer.folders : []) {
      const folder = raw as Record<string, unknown>;
      const id = text(folder.id, "");
      if (!id) continue;
      const option = document.createElement("option");
      option.value = id;
      option.textContent = text(folder.label, id);
      peerFolder.append(option);
    }
  };
  renderPeerFolders();
  peer.onchange = renderPeerFolders;
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
      const folderRows = (Array.isArray(computer.folders) ? computer.folders : []).map((rawFolder) => {
        const folder = rawFolder as Record<string, unknown>;
        const folderId = text(folder.id, "");
        return `<div class="backup-folder-row"><span><strong>${escapeHtml(text(folder.label, folderId))}</strong><small>${escapeHtml(text(folder.remote_path, ""))}</small></span><button type="button" class="secondary" data-action="use-remote-backup" data-machine="${escapeHtml(machineKey)}" data-folder="${escapeHtml(folderId)}">Browse / Receive</button></div>`;
      }).join("");
      const item = document.createElement("article");
      item.className = "item";
      item.innerHTML = `
        <div class="item-heading">
          <strong>${escapeHtml(text(computer.machine_label, machineKey))}</strong>
          <span class="pill ${linkedProfile ? "is-linked" : ""}">${linkedProfile ? "Also local" : "Remote only"}</span>
        </div>
        <span>${folders} folder(s)</span>
        <span>${escapeHtml(text(computer.updated_at, text(computer.generated_at)))}</span>
        <div class="backup-folder-list">${folderRows}</div>`;
      computerList.append(item);
    }
    if (latestComputers.length === 0) computerList.textContent = "No remote computers published yet";
  }
}

function useRemoteBackup(button: HTMLElement): void {
  if (!transferForm) return;
  activateTab("transfer");
  renderTransferOptions();
  const computerSelect = formField(transferForm, "source_computer") as HTMLSelectElement | null;
  const folderSelect = formField(transferForm, "source_folder") as HTMLSelectElement | null;
  if (!computerSelect || !folderSelect) return;
  computerSelect.value = button.dataset.machine ?? "";
  computerSelect.dispatchEvent(new Event("change"));
  const computer = remoteComputerByMachineId(computerSelect.value);
  const selectedFolder = (Array.isArray(computer?.folders) ? computer.folders : []).find((raw) => text((raw as Record<string, unknown>).id, "") === button.dataset.folder) as Record<string, unknown> | undefined;
  const selectedRemotePath = text(selectedFolder?.remote_path, "");
  if (selectedRemotePath) folderSelect.value = remoteRoot(latestConfig?.remote_base ?? "dropbox:", selectedRemotePath);
  folderSelect.dispatchEvent(new Event("change"));
  setMessage("Remote backup opened in Receive", "ok");
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
  const configuredLogLevel = text(config.logging?.temporary_level, text(config.logging?.level, "normal"));
  const logLevelInput = logLevelForm?.elements.namedItem("level") as HTMLSelectElement | null;
  if (logLevelInput && ["quiet", "normal", "debug", "trace"].includes(configuredLogLevel)) {
    logLevelInput.value = configuredLogLevel;
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
  renderLinkAndHistoryOptions();
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
    renderLinkAndHistoryOptions();
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
  const clone = (transferForm.elements.namedItem("clone") as HTMLInputElement | null)?.checked ?? false;
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
    showUiCommand([clone ? "receive" : "pull", transferSourceRoot, destination, ...(clone ? ["--clone"] : []), ...(dryRun ? ["--dry-run"] : []), ...[...selectedTransferPaths].flatMap((path) => ["--select", path])]);
    const result = await invoke<CommandResult>("pull_remote", {
      source: transferSourceRoot,
      destination,
      dryRun,
      clone,
      selectedPaths: [...selectedTransferPaths],
    });
    transferOutput.textContent = dryRun
      ? formatComparisonOutput(result.output)
      : `${result.output || "receive job queued"}\nThe daemon stages it after any active backup. Open Jobs to review and apply; the destination remains unchanged until then.`;
    setMessage(dryRun ? "Comparison complete" : "Receive job queued", "ok");
    holdAction("transfer");
  } catch (error) {
    transferOutput.textContent = String(error);
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

function formatComparisonOutput(output: string): string {
  try {
    const comparison = JSON.parse(output) as Record<string, unknown>;
    const counts = (comparison.counts ?? {}) as Record<string, unknown>;
    const summary = Object.entries(counts)
      .filter(([, count]) => Number(count) > 0)
      .map(([category, count]) => `${category.replace(/_/g, " ")}: ${Number(count)}`)
      .join(" · ");
    const results = Array.isArray(comparison.results) ? comparison.results as Array<Record<string, unknown>> : [];
    const changed = results.filter((item) => !["same", "same_change"].includes(text(item.category, "")));
    const lines = changed.slice(0, 200).map((item) => `${text(item.category, "different").replace(/_/g, " ")}  ${text(item.path, "-")}`);
    return [summary || "No differences found.", ...lines, ...(changed.length > 200 ? [`… ${changed.length - 200} more`] : [])].join("\n");
  } catch {
    return output || "No differences found.";
  }
}

function jobDecisionOptions(category: string): string[] {
  if (category === "peer_only") return ["add", "leave_staged"];
  if (category === "local_only") return ["keep_local", "delete"];
  if (category === "different" || category === "conflict") return ["keep_local", "keep_both", "replace", "leave_staged"];
  return ["same"];
}

function jobDecisionLabel(policy: string): string {
  return ({
    add: "Restore into watched folder",
    leave_staged: "Keep only in staging",
    keep_local: "Keep current local file",
    keep_both: "Keep both files",
    replace: "Replace local with this version",
    delete: "Delete local file",
    same: "No change needed",
  } as Record<string, string>)[policy] ?? policy.replace(/_/g, " ");
}

function renderJobs(): void {
  if (!jobList) return;
  jobList.innerHTML = "";
  if (latestJobs.length === 0) {
    jobList.textContent = "No receive jobs yet.";
    return;
  }
  for (const job of latestJobs) {
    const id = text(job.id, "unknown");
    const status = text(job.status, "unknown");
    const comparison = (job.comparison ?? {}) as Record<string, unknown>;
    const results = Array.isArray(comparison.results) ? comparison.results as Array<Record<string, unknown>> : [];
    const decisions = results.filter((item) => !["same", "same_change"].includes(text(item.category, "")));
    const item = document.createElement("article");
    item.className = "card job-card";
    item.dataset.jobId = id;
    const decisionRows = decisions.map((decision) => {
      const path = text(decision.path, "");
      const category = text(decision.category, "different");
      const options = jobDecisionOptions(category)
        .map((option) => `<option value="${escapeHtml(option)}">${escapeHtml(jobDecisionLabel(option))}</option>`)
        .join("");
      return `<label class="job-decision"><span class="path">${escapeHtml(path)}</span><span class="pill">${escapeHtml(category)}</span><select data-job-policy data-path="${escapeHtml(path)}">${options}</select></label>`;
    }).join("");
    const canApply = status === "ready" || status === "needs_review" || status === "interrupted";
    const canRollback = ["complete", "interrupted", "needs_review"].includes(status);
    const recoveryComparison = (job.recovery_compare ?? {}) as Record<string, unknown>;
    const isRecovery = job.source_kind === "dropbox_revision";
    const recoveryPreview = isRecovery
      ? `<div class="recovery-preview"><p><strong>Staged and verified:</strong> ${escapeHtml(text(recoveryComparison.summary, "Dropbox revision is ready"))}</p>${recoveryComparison.unified_diff ? `<pre>${escapeHtml(text(recoveryComparison.unified_diff, ""))}</pre>` : ""}<p class="reason">Open the staging folder to inspect the downloaded copy. To recover it, choose the destination action below and select Restore Selected Version. Backup remains paused until you resume it from Recovery.</p></div>`
      : "";
    item.innerHTML = `
      <div class="section-title"><h3>${escapeHtml(text(job.source_label, "Receive"))}</h3><span class="pill">${escapeHtml(status)}</span></div>
      <p class="path">${escapeHtml(text(job.source, "-"))} → ${escapeHtml(text(job.destination, "-"))}</p>
      ${recoveryPreview}
      <div class="job-decisions">${decisionRows || "<p>No conflict decisions are required.</p>"}</div>
      <div class="actions left">
        <button type="button" class="secondary" data-action="show-job">Details</button>
        ${isRecovery ? '<button type="button" class="secondary" data-action="open-job-staging">Open Staging Folder</button><button type="button" class="secondary" data-action="open-job-destination">Open Watched Folder</button>' : ""}
        ${canApply ? `<button type="button" class="primary" data-action="apply-job">${isRecovery ? "Restore Selected Version" : "Apply Reviewed Choices"}</button>` : ""}
        ${status === "interrupted" ? '<button type="button" class="secondary" data-action="reconcile-job">Reconcile</button>' : ""}
        ${canRollback ? '<button type="button" class="secondary" data-action="rollback-job">Roll Back</button>' : ""}
      </div>`;
    jobList.append(item);
  }
}

async function openJobFolder(button: HTMLElement, kind: "staging" | "destination"): Promise<void> {
  const jobId = button.closest<HTMLElement>("[data-job-id]")?.dataset.jobId ?? "";
  const job = latestJobs.find((item) => text(item.id, "") === jobId);
  const paths = (job?.paths ?? {}) as Record<string, unknown>;
  const path = kind === "staging" ? text(paths.staging, "") : text(job?.destination, "");
  if (!path) {
    setMessage(`${kind === "staging" ? "Staging" : "Destination"} folder is unavailable`, "error");
    return;
  }
  setBusy(kind === "staging" ? "open-job-staging" : "open-job-destination");
  try {
    await invoke("open_local_folder", { path });
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function loadJobs(): Promise<void> {
  setBusy("jobs");
  try {
    latestJobs = await invoke<Array<Record<string, unknown>>>("get_jobs");
    jobsLoaded = true;
    renderJobs();
    setMessage("Receive jobs loaded", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function runJobAction(button: HTMLElement, action: "apply" | "reconcile" | "rollback"): Promise<void> {
  const card = button.closest<HTMLElement>("[data-job-id]");
  const jobId = card?.dataset.jobId ?? "";
  if (!jobId) return;
  const policies = action === "apply"
    ? [...(card?.querySelectorAll<HTMLSelectElement>("[data-job-policy]") ?? [])].map((select) => `${select.dataset.path ?? ""}=${select.value}`)
    : [];
  setBusy("jobs");
  try {
    showUiCommand(["jobs", action, jobId, ...policies.flatMap((policy) => ["--policy", policy])]);
    const result = await invoke<CommandResult>("job_operation", { action, jobId, policies });
    if (jobOutput) jobOutput.textContent = result.output || `${action} queued`;
    setMessage(`${action} queued`, "ok");
    window.setTimeout(() => void loadJobs(), 800);
  } catch (error) {
    if (jobOutput) jobOutput.textContent = String(error);
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

function showJob(button: HTMLElement): void {
  const id = button.closest<HTMLElement>("[data-job-id]")?.dataset.jobId;
  const job = latestJobs.find((item) => item.id === id);
  if (jobOutput) jobOutput.textContent = JSON.stringify(job ?? {}, null, 2);
}

function renderLinks(values: Array<Record<string, unknown>>, statusMode: boolean): void {
  if (!linkList) return;
  linkList.innerHTML = "";
  if (values.length === 0) {
    linkList.textContent = "No linked folders yet.";
    return;
  }
  for (const raw of values) {
    const link = (statusMode ? raw.link : raw) as Record<string, unknown>;
    const local = (link.local ?? {}) as Record<string, unknown>;
    const peer = (link.peer ?? {}) as Record<string, unknown>;
    const comparison = (raw.comparison ?? {}) as Record<string, unknown>;
    const counts = (comparison.counts ?? {}) as Record<string, unknown>;
    const countSummary = Object.entries(counts).filter(([, count]) => Number(count) > 0).map(([name, count]) => `${name.replace(/_/g, " ")}: ${Number(count)}`).join(" · ");
    const item = document.createElement("article");
    item.className = "card";
    item.dataset.linkId = text(link.id, "");
    item.innerHTML = `
      <div class="section-title"><h3>${escapeHtml(text(link.label, "Linked folder"))}</h3><span class="pill">${escapeHtml(text(statusMode ? raw.status : link.status, "not checked"))}</span></div>
      <p class="path">Local: ${escapeHtml(text(local.folder_id, "-"))}/${escapeHtml(text(local.subpath, ""))}</p>
      <p class="path">Peer: ${escapeHtml(text(peer.machine_id, "-"))}/${escapeHtml(text(peer.folder_id, "-"))}/${escapeHtml(text(peer.subpath, ""))}</p>
      ${countSummary ? `<p>${escapeHtml(countSummary)}</p>` : ""}
      <div class="actions left"><button type="button" class="primary" data-action="review-link">Review &amp; Sync</button><button type="button" class="secondary" data-action="remove-link">Remove Link</button></div>`;
    linkList.append(item);
  }
}

async function loadLinks(refreshStatus = false): Promise<void> {
  setBusy("links");
  try {
    const values = await invoke<Array<Record<string, unknown>>>("get_links", { refreshStatus });
    linksLoaded = true;
    renderLinks(values, refreshStatus);
    setMessage(refreshStatus ? "Linked-folder changes checked" : "Linked folders loaded", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function addLink(event: SubmitEvent): Promise<void> {
  event.preventDefault();
  if (!addLinkForm) return;
  const request = {
    local_folder: selectedValue(addLinkForm, "local_folder"),
    peer_machine: selectedValue(addLinkForm, "peer_machine"),
    peer_folder: selectedValue(addLinkForm, "peer_folder"),
    local_subpath: inputValue(addLinkForm, "local_subpath"),
    peer_subpath: inputValue(addLinkForm, "peer_subpath"),
    label: inputValue(addLinkForm, "label"),
  };
  setBusy("links");
  try {
    showUiCommand(["links", "add", request.local_folder, request.peer_machine, request.peer_folder, ...(request.local_subpath ? ["--local-subpath", request.local_subpath] : []), ...(request.peer_subpath ? ["--peer-subpath", request.peer_subpath] : [])]);
    await invoke("add_link", { request });
    await loadLinks(false);
    setMessage("Linked folder activated", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function removeLink(button: HTMLElement): Promise<void> {
  const linkId = button.closest<HTMLElement>("[data-link-id]")?.dataset.linkId ?? "";
  if (!linkId) return;
  try {
    showUiCommand(["links", "remove", linkId]);
    await invoke("remove_link", { linkId });
    await loadLinks(false);
  } catch (error) {
    setMessage(String(error), "error");
  }
}

async function reviewLink(button: HTMLElement): Promise<void> {
  const linkId = button.closest<HTMLElement>("[data-link-id]")?.dataset.linkId ?? "";
  if (!linkId) return;
  try {
    showUiCommand(["links", "review", linkId]);
    const result = await invoke<CommandResult>("review_link", { linkId });
    jobsLoaded = false;
    setMessage(result.output || "Linked-folder review queued", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  }
}

function selectedRestoreFolder(): FolderView | null {
  if (!latestConfig || !historyFolder) return null;
  return (latestConfig.folders.find((raw) => text((raw as FolderView).id, "") === historyFolder.value) as FolderView | undefined) ?? null;
}

function renderRestoreFolder(): void {
  const target = (latestRecovery?.target ?? {}) as Record<string, unknown>;
  const remoteCopy = (latestRecovery?.cancel_remote_copy ?? {}) as Record<string, unknown>;
  const folder = selectedRestoreFolder();
  const active = latestRecovery?.active === true;
  const hasTarget = active && text(target.folder_id, "") !== "";
  if (restoreLocal) restoreLocal.textContent = hasTarget ? text(target.local_path, "-") : text(folder?.local_path, "Choose a tracked folder");
  if (restoreRemote) restoreRemote.textContent = hasTarget ? text(target.remote_root, "-") : text(folder?.remote_root, "Choose a tracked folder");
  if (restoreDestination) restoreDestination.textContent = hasTarget ? text(latestRecovery?.destination, "-") : "Created after Recovery Mode starts";
  const hasRemoteCopy = active && text(remoteCopy.destination, "") !== "";
  if (cancelRemoteCopyFact) cancelRemoteCopyFact.hidden = !hasRemoteCopy;
  if (cancelRemoteCopyDestination) cancelRemoteCopyDestination.textContent = hasRemoteCopy ? text(remoteCopy.destination, "-") : "-";
  if (historyFolder) historyFolder.disabled = hasTarget;
}

function updateRestoreActions(status: Record<string, unknown>): void {
  const active = status.active === true;
  const phase = text(status.phase, "inactive");
  const locked = status.locked === true;
  const hasFolder = selectedRestoreFolder() !== null;
  const isLegacy = phase === "legacy_locked";
  const canCancel = active && Number(status.schema_version ?? 0) >= 2 && !["legacy_locked", "invalid_locked"].includes(phase);
  const waitingForLock = phase === "entering" || status.draining === true;
  const remoteCopy = (status.cancel_remote_copy ?? {}) as Record<string, unknown>;
  const remoteCopyStatus = text(remoteCopy.status, "");
  if (recoveryStatusNotice) recoveryStatusNotice.hidden = !canCancel;
  if (recoveryCancelActions) recoveryCancelActions.hidden = !canCancel;
  if (recoveryStatusSummary && canCancel) {
    const target = (status.target ?? {}) as Record<string, unknown>;
    const targetLabel = text(target.label, text(target.folder_id, "Selected backup"));
    const lastError = text(status.last_error, "");
    recoveryStatusSummary.textContent = waitingForLock
      ? `${targetLabel} is entering Recovery Mode. Safe Sync is finishing the current folder operation before locking every outbound backup; these safety actions become available after the lock completes.`
      : remoteCopyStatus === "failed"
      ? `Dropbox safety copy failed: ${text(remoteCopy.last_error, "unknown error")}. Recovery Mode remains locked; review the destination and retry.`
      : phase === "cancel_failed" && lastError
      ? `Cancel failed: ${lastError}. Backups remain locked; retry Cancel after reviewing the problem.`
      : remoteCopyStatus === "verified"
        ? `${targetLabel} is locked. Its Dropbox state was saved and verified locally (${Number(remoteCopy.entry_count ?? 0).toLocaleString()} entries, ${formatBytes(Number(remoteCopy.byte_count ?? 0))}); inspect it or replace Dropbox from local and resume.`
        : `${targetLabel} is locked. Save Dropbox locally first if you may need its current state, or explicitly replace it from local and resume.`;
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-action='cancel-recovery']")) {
    // The barrier reports a live cancellation as draining. Persisted
    // cancel_* phases with no barrier holder indicate an interrupted attempt
    // and must remain retryable after restart.
    button.disabled = !canCancel || waitingForLock;
    button.title = waitingForLock ? "Available after the current folder operation finishes and Recovery Mode is locked." : "";
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-action='save-recovery-remote-copy']")) {
    button.disabled = !canCancel || waitingForLock || remoteCopyStatus === "verified";
    button.title = waitingForLock ? "Available after the current folder operation finishes and Recovery Mode is locked." : "";
    button.textContent = remoteCopyStatus === "failed" ? "Retry Saving Dropbox Copy" : remoteCopyStatus === "exporting" ? "Saving Dropbox Copy" : "Save Dropbox Copy Locally";
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-action='open-recovery-remote-copy']")) {
    button.hidden = remoteCopyStatus !== "verified";
    button.disabled = remoteCopyStatus !== "verified";
  }
  if (recoveryLegacyActions) recoveryLegacyActions.hidden = !isLegacy;
  if (recoveryClearLegacyButton) recoveryClearLegacyButton.disabled = !isLegacy || !locked;
  if (recoveryEnterButton) recoveryEnterButton.disabled = active || !hasFolder;
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-action='open-recovery-dropbox']")) {
    const stage = button.dataset.recoveryDropboxStage;
    button.disabled = !active || !locked || (stage === "rewind" ? phase !== "locked" : phase !== "exported");
  }
  if (recoveryRewoundButton) recoveryRewoundButton.disabled = phase !== "locked";
  if (recoveryExportButton) recoveryExportButton.disabled = !["rewound", "export_failed", "exporting"].includes(phase);
  if (recoveryOpenExportButton) recoveryOpenExportButton.disabled = !["exported", "undo_complete", "verification_failed", "verified"].includes(phase);
  if (recoveryUndoButton) recoveryUndoButton.disabled = !["exported", "undo_complete", "verification_failed"].includes(phase);
  if (recoveryVerifyButton) recoveryVerifyButton.disabled = !["undo_complete", "verification_failed", "verified"].includes(phase);
  if (recoveryExitButton) recoveryExitButton.disabled = phase !== "verified";
  if (recoveryRemoveAllButton) {
    const removableCount = latestRecoveryDownloads.filter((item) => item.available !== true || item.deletable === true).length;
    recoveryRemoveAllButton.disabled = active || removableCount === 0;
    recoveryRemoveAllButton.title = active ? "Finish or safely cancel Recovery Mode before deleting downloaded copies." : "";
  }
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-action='remove-recovery-download']")) {
    const available = button.dataset.recoveryDownloadAvailable === "true";
    const deletable = button.dataset.recoveryDownloadDeletable === "true";
    button.disabled = active || (available && !deletable);
    button.title = active
      ? "Finish or safely cancel Recovery Mode before deleting downloaded copies."
      : available && !deletable
        ? "This custom location must be deleted manually."
        : "";
  }
}

function renderRecoveryStatus(value: Record<string, unknown>): Record<string, unknown> {
  latestRecovery = value;
  const phase = text(value.phase, "inactive");
  const labels: Record<string, string> = {
    inactive: "Backup active",
    entering: "Locking backups",
    locked: "Recovery locked",
    rewound: "Ready to export",
    exporting: "Exporting history",
    export_failed: "Export needs attention",
    exported: "Historical copy ready",
    undo_complete: "Ready to verify",
    verification_failed: "Current state differs",
    verified: "Verified — safe to exit",
      legacy_locked: "Legacy recovery lock",
      invalid_locked: "Recovery state damaged — locked",
      cancel_checking: "Checking before cancel",
      canceling: "Restoring Dropbox from local",
      cancel_failed: "Cancel needs attention — locked",
      cancel_verified: "Cancel verified",
  };
  const entering = value.active === true && phase === "entering";
  const enteredAt = Date.parse(text(value.entered_at, ""));
  const elapsedSeconds = Number.isFinite(enteredAt) ? Math.max(0, Math.floor((Date.now() - enteredAt) / 1000)) : null;
  const activeFolder = text(latestStatus?.sync_state?.current_folder_label, "the current folder");
  const entryProgressMessage = `Finishing ${activeFolder} before Recovery Mode locks${elapsedSeconds === null ? "" : ` · waiting ${elapsedSeconds.toLocaleString()}s`}`;
  for (const progress of recoveryEntryProgress) progress.hidden = !entering;
  for (const progressText of recoveryEntryProgressText) progressText.textContent = entryProgressMessage;
  if (recoveryState) {
    recoveryState.textContent = labels[phase] ?? phase;
    recoveryState.classList.toggle("is-active", value.active === true);
  }
  if (value.active === true) {
    const target = (value.target ?? {}) as Record<string, unknown>;
    const targetLabel = text(target.label, text(target.folder_id, "Selected backup"));
    if (phase === "entering") {
      if (stateLabel) stateLabel.textContent = "Entering Recovery Mode";
      if (reasonLabel) reasonLabel.textContent = `Finishing ${activeFolder} before locking every outbound backup`;
      if (syncLabel) syncLabel.textContent = "finishing current sync";
      statusDot?.setAttribute("aria-label", "Entering Recovery Mode");
    } else if (value.locked === true) {
      if (stateLabel) stateLabel.textContent = "Recovery Mode locked";
      if (reasonLabel) reasonLabel.textContent = `${targetLabel}: ${labels[phase] ?? phase}`;
      if (syncLabel) syncLabel.textContent = "blocked";
      statusDot?.setAttribute("aria-label", "Recovery Mode locked");
    }
  }
  if (recoveryGuidance) {
    const guidance: Record<string, string> = {
      inactive: "Choose a folder and enter Recovery Mode before changing Dropbox history.",
      entering: "A current folder operation is finishing. Do not use Dropbox Rewind until this says Recovery locked.",
      locked: "All Safe Sync outbound backups are locked. Rewind only the selected Dropbox folder, then wait for its completion email.",
      rewound: "Dropbox Rewind is confirmed complete. Create the isolated historical copy now.",
      exporting: "The historical folder is being copied and verified. Keep Dropbox unchanged.",
      export_failed: "The isolated export did not verify. Keep Recovery Mode active, wait for Dropbox to stabilize, and retry.",
      exported: "The historical copy is safe locally. Undo the Dropbox Rewind and wait for its completion email.",
      undo_complete: "Dropbox reports that undo-Rewind finished. Verify current Dropbox against the unchanged local folder.",
      verification_failed: "Dropbox is still changing or differs from local. Keep Recovery Mode locked and resolve or retry verification.",
      verified: "Current Dropbox and local state match. Exit will run the same verification once more before unlocking backups.",
        legacy_locked: "An old-format pause is safely blocking backups. If Dropbox was not rewound and no recovery is underway, clear it here to resume normal backups.",
        invalid_locked: "Recovery state is damaged. Backups remain safely locked; inspect logs and use the headless emergency force-exit only after manually proving safety.",
        cancel_checking: "Safe Sync is checking whether Dropbox already matches local before cancelling.",
        canceling: "Safe Sync is restoring the selected Dropbox backup from the current local folder. Recovery Mode remains locked.",
        cancel_failed: value.last_error ? `Cancel failed: ${text(value.last_error)}. Backups remain safely locked; retry after reviewing the problem.` : "Cancellation did not complete. Backups remain safely locked; review the error and retry.",
        cancel_verified: "Dropbox matches local. Safe Sync is completing the guarded unlock.",
    };
    recoveryGuidance.textContent = guidance[phase] ?? text(value.instructions, "Recovery Mode remains locked.");
  }
  const verification = (value.verification ?? {}) as Record<string, unknown>;
  if (recoveryVerification) {
    const counts = (verification.counts ?? {}) as Record<string, unknown>;
    recoveryVerification.textContent = verification.checked_at
      ? verification.equal === true
        ? `Verified ${Number(verification.local_entries ?? 0).toLocaleString()} entries; Dropbox is stable and matches local.`
        : `Not equal: ${Number(counts.local_only ?? 0).toLocaleString()} local-only, ${Number(counts.peer_only ?? 0).toLocaleString()} Dropbox-only, ${Number(counts.different ?? 0).toLocaleString()} different.`
      : "";
  }
  renderRestoreFolder();
  updateRestoreActions(value);
  return value;
}

async function refreshRecoveryStatus(): Promise<Record<string, unknown>> {
  if (recoveryActionInFlight) return latestRecovery ?? {};
  try {
    const value = await invoke<Record<string, unknown>>("get_recovery_status");
    if (recoveryActionInFlight) return latestRecovery ?? value;
    return renderRecoveryStatus(value);
  } catch (error) {
    if (recoveryState) recoveryState.textContent = "Status unavailable";
    throw error;
  }
}

function renderRecoveryDownloads(downloads: Array<Record<string, unknown>>): void {
  if (!recoveryDownloadList) return;
  latestRecoveryDownloads = [...downloads];
  const direction = recoveryDownloadSort?.value === "oldest" ? 1 : -1;
  const sorted = [...downloads].sort((left, right) => {
    const leftTime = Date.parse(text(left.completed_at, text(left.created_at, ""))) || 0;
    const rightTime = Date.parse(text(right.completed_at, text(right.created_at, ""))) || 0;
    return (leftTime - rightTime) * direction;
  });
  if (recoveryRemoveAllButton) {
    const removableCount = downloads.filter((item) => item.available !== true || item.deletable === true).length;
    recoveryRemoveAllButton.disabled = latestRecovery?.active === true || removableCount === 0;
  }
  if (sorted.length === 0) {
    recoveryDownloadList.innerHTML = '<p class="empty">No verified recovery copies have been downloaded yet.</p>';
    return;
  }
  recoveryDownloadList.innerHTML = sorted.map((download) => {
    const kind = text(download.kind, "") === "historical_recovery_copy" ? "Historical recovery copy" : "Dropbox safety copy";
    const completedRaw = text(download.completed_at, "");
    const completed = completedRaw ? new Date(completedRaw).toLocaleString() : "Time unavailable";
    const available = download.available === true;
    const entryCount = download.entry_count === null || download.entry_count === undefined ? null : Number(download.entry_count);
    const byteCount = download.byte_count === null || download.byte_count === undefined ? null : Number(download.byte_count);
    const copyFacts = [
      entryCount !== null && Number.isFinite(entryCount) ? `${entryCount.toLocaleString()} entries` : "entry count unavailable",
      byteCount !== null && Number.isFinite(byteCount) ? formatBytes(byteCount) : "size unavailable",
    ].join(" · ");
    const destination = text(download.destination, "-");
    const folder = text(download.folder_label, text(download.folder_id, "Unknown folder"));
    const deletable = download.deletable === true;
    const removalLabel = available ? deletable ? "Delete Local Copy" : "Delete Manually" : "Remove Record";
    const removalDisabled = (available && !deletable) || latestRecovery?.active === true;
    return `<article class="recovery-download" data-recovery-download-id="${escapeHtml(text(download.id, ""))}" data-recovery-download-path="${escapeHtml(destination)}">
      <div class="recovery-download-summary">
        <div><h4>${escapeHtml(folder)}</h4><p class="reason">${escapeHtml(completed)} · ${escapeHtml(copyFacts)}</p></div>
        <span class="pill">${escapeHtml(kind)}</span>
      </div>
      <details><summary>Location and Dropbox source</summary><p class="path">${escapeHtml(destination)}</p><p class="reason">Dropbox source: ${escapeHtml(text(download.remote_root, "-"))}</p></details>
      <div class="actions left recovery-download-actions">
        <button type="button" class="secondary" data-action="open-recovery-download" ${available ? "" : "disabled"}>${available ? "Open Folder" : "Folder Missing"}</button>
        <button type="button" class="secondary danger" data-action="remove-recovery-download" data-recovery-download-available="${available}" data-recovery-download-deletable="${deletable}" data-recovery-control ${removalDisabled ? "disabled" : ""}>${removalLabel}</button>
      </div>
    </article>`;
  }).join("");
}

async function loadRecoveryDownloads(): Promise<void> {
  if (!recoveryDownloadList) return;
  recoveryDownloadList.innerHTML = '<p class="empty">Loading downloaded copies…</p>';
  try {
    renderRecoveryDownloads(await invoke<Array<Record<string, unknown>>>("get_recovery_downloads"));
  } catch (error) {
    recoveryDownloadList.innerHTML = `<p class="empty">Could not load downloaded copies: ${escapeHtml(String(error))}</p>`;
  }
}

async function openRecoveryDownload(button: HTMLElement): Promise<void> {
  const destination = button.closest<HTMLElement>("[data-recovery-download-path]")?.dataset.recoveryDownloadPath ?? "";
  if (!destination) return;
  try {
    await invoke("open_local_folder", { path: destination });
    setMessage("Opened downloaded recovery copy", "ok");
  } catch (error) {
    setMessage(String(error), "error");
    await loadRecoveryDownloads();
  }
}

async function removeRecoveryDownload(button: HTMLElement | null, removeAll = false): Promise<void> {
  if (latestRecovery?.active === true) {
    setMessage("Finish or safely cancel Recovery Mode before deleting downloaded copies", "error");
    return;
  }
  const card = button?.closest<HTMLElement>("[data-recovery-download-id]");
  const downloadId = card?.dataset.recoveryDownloadId ?? "";
  const destination = card?.dataset.recoveryDownloadPath ?? "";
  const candidates = latestRecoveryDownloads.filter((item) => item.available !== true || item.deletable === true);
  if (removeAll && candidates.length === 0) {
    setMessage("There are no managed local recovery copies to delete", "error");
    return;
  }
  const prompt = removeAll
    ? `Permanently delete all ${candidates.length.toLocaleString()} managed local recovery copies and clear their list records? Watched folders and Dropbox will not be changed. This cannot be undone by Safe Sync.`
    : `Permanently delete this local recovery copy and remove its list record?\n\n${destination}\n\nWatched folders and Dropbox will not be changed. This cannot be undone by Safe Sync.`;
  if (!window.confirm(prompt)) return;
  showRecoveryOperation("remove-downloads");
  setBusy("history");
  try {
    const result = await invoke<Record<string, unknown>>("remove_recovery_download", {
      downloadId: removeAll ? null : downloadId,
      all: removeAll,
    });
    const downloads = Array.isArray(result.downloads) ? result.downloads as Array<Record<string, unknown>> : [];
    const removed = Array.isArray(result.removed) ? result.removed.length : 0;
    const skipped = Array.isArray(result.skipped) ? result.skipped.length : 0;
    renderRecoveryDownloads(downloads);
    setMessage(
      skipped > 0 ? `Deleted ${removed.toLocaleString()} local copies; ${skipped.toLocaleString()} custom locations require manual deletion` : `Deleted ${removed.toLocaleString()} local recovery ${removed === 1 ? "copy" : "copies"}`,
      skipped > 0 ? "error" : "ok",
    );
  } catch (error) {
    setMessage(String(error), "error");
    await loadRecoveryDownloads();
  } finally {
    hideRecoveryOperation();
    setBusy(null);
  }
}

function recoveryStatusNeedsRefresh(status: SafeSyncStatus | null): boolean {
  const restoreOpen = document.querySelector<HTMLElement>("[data-view='history']")?.classList.contains("is-active") === true;
  return latestRecovery?.active === true || (status !== null && syncState(status) === "recovery_paused") || restoreOpen;
}

type RecoveryAction = "enter" | "clear-legacy" | "cancel" | "save-remote-copy" | "mark-rewound" | "export" | "mark-undo-complete" | "verify" | "exit";

async function controlRecovery(action: RecoveryAction): Promise<void> {
  const confirmations: Partial<Record<RecoveryAction, string>> = {
    "clear-legacy": "Clear the old recovery pause and allow backups again? Continue only if you did not Rewind Dropbox and no recovery is in progress.",
    cancel: "Replace Dropbox from the current local folder and resume normal backup? Safe Sync verifies first and writes only if they differ. Any verified Dropbox safety copy shown in this screen remains local; if you skipped it, the current remote state is not downloaded by Safe Sync. Continue?",
    "save-remote-copy": "Save the CURRENT DROPBOX folder into a separate local recovery folder? Safe Sync will verify the complete download and keep Recovery Mode locked. This may require substantial disk space and time.",
    "mark-rewound": "Confirm only after Dropbox emailed that the historical Rewind finished. Continue?",
    "mark-undo-complete": "Confirm only after Dropbox emailed that undo-Rewind finished. Continue?",
    exit: "Exit Recovery Mode? Safe Sync will verify Dropbox and local again before unlocking every queued backup.",
  };
  if (confirmations[action] && !window.confirm(confirmations[action])) return;
  const folder = action === "enter" ? selectedRestoreFolder()?.id : undefined;
  if (action === "enter" && !folder) {
    setMessage("Choose a configured backup folder first", "error");
    return;
  }
  recoveryActionInFlight = true;
  showRecoveryOperation(action);
  setBusy("history");
  try {
    const cliAction = action === "enter"
      ? ["recovery", "enter", String(folder)]
      : action === "clear-legacy"
        ? ["recovery", "clear-legacy", "--confirm", "CLEAR-OLD-PAUSE"]
        : action === "cancel"
          ? ["recovery", "cancel", "--confirm", "REPLACE-DROPBOX-WITH-LOCAL"]
        : action === "save-remote-copy"
          ? ["recovery", "save-remote-copy"]
        : ["recovery", action];
    showUiCommand(cliAction);
    const value = await invoke<Record<string, unknown>>("control_recovery", { action, folder: folder ?? null });
    renderRecoveryStatus(value);
    if (["save-remote-copy", "export"].includes(action)) await loadRecoveryDownloads();
    if (["cancel", "clear-legacy", "exit"].includes(action)) await refreshStatusQuietly();
    const messages: Record<RecoveryAction, string> = {
      enter: value.current_operation_finishes_before_lock === true ? "Recovery Mode entered; waiting for the current folder operation to finish" : "Recovery Mode locked every outbound backup",
      "clear-legacy": "Old recovery pause cleared; normal backups may resume",
      cancel: value.remote_reconciled === true ? "Recovery cancelled after Dropbox was restored from local and verified" : "Recovery cancelled; Dropbox already matched local, so no files were transferred",
      "save-remote-copy": "Current Dropbox folder downloaded and verified locally; open the saved copy before deciding whether to resume",
      "mark-rewound": "Dropbox Rewind completion recorded; historical export is now enabled",
      export: "Historical folder copied and verified in isolated local staging",
      "mark-undo-complete": "Dropbox undo-Rewind completion recorded; current-state verification is now enabled",
      verify: value.phase === "verified" ? "Dropbox is stable and matches local" : "Dropbox does not yet match local; Recovery Mode remains locked",
      exit: "Recovery Mode exited after final verification; queued backup work may resume",
    };
    setMessage(messages[action], value.phase === "verification_failed" ? "error" : "ok");
  } catch (error) {
    recoveryActionInFlight = false;
    try {
      await refreshRecoveryStatus();
    } catch {
      // Preserve the original action error when status refresh also fails.
    }
    setMessage(String(error), "error");
  } finally {
    recoveryActionInFlight = false;
    hideRecoveryOperation();
    setBusy(null);
  }
}

async function openRecoveryDropbox(): Promise<void> {
  const status = await refreshRecoveryStatus();
  if (status.locked !== true) {
    setMessage("Wait until Recovery Mode says Recovery locked", "error");
    return;
  }
  const target = (status.target ?? {}) as Record<string, unknown>;
  const remoteRoot = text(target.remote_root, "");
  if (!remoteRoot) {
    setMessage("Recovery Mode has no selected Dropbox folder", "error");
    return;
  }
  try {
    await invoke("open_dropbox_location", { request: { remoteRoot } });
    setMessage("Dropbox folder opened. Use Folder settings → Rewind this folder, then wait for Dropbox to finish.", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  }
}

async function openRecoveryExport(): Promise<void> {
  const status = await refreshRecoveryStatus();
  const destination = text(status.destination, "");
  if (!destination || !["exported", "undo_complete", "verification_failed", "verified"].includes(text(status.phase, ""))) {
    setMessage("Create and verify the historical copy first", "error");
    return;
  }
  try {
    await invoke("open_local_folder", { path: destination });
  } catch (error) {
    setMessage(String(error), "error");
  }
}

async function openCancelRemoteCopy(): Promise<void> {
  const status = await refreshRecoveryStatus();
  const remoteCopy = (status.cancel_remote_copy ?? {}) as Record<string, unknown>;
  const destination = text(remoteCopy.destination, "");
  if (text(remoteCopy.status, "") !== "verified" || !destination) {
    setMessage("Save and verify the Dropbox copy first", "error");
    return;
  }
  try {
    await invoke("open_local_folder", { path: destination });
    setMessage("Opened the verified Dropbox safety copy. Recovery Mode remains locked.", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  }
}

/* Legacy snapshot-history UI removed in favor of Dropbox-owned Rewind.
async function loadRecoveryRecent(): Promise<void> {
  if (!recoveryRecentList) return;
  setBusy("history");
  recoveryRecentList.innerHTML = '<p class="empty">Loading recent successful backups…</p>';
  try {
    const folder = recoveryRecentFolder?.value || null;
    showUiCommand(["recovery", "recent", ...(folder ? ["--folder", folder] : [])]);
    const value = await invoke<Record<string, unknown>>("get_recovery_recent", { folder });
    const cycles = Array.isArray(value.cycles) ? value.cycles as Array<Record<string, unknown>> : [];
    recoveryRecentList.innerHTML = "";
    for (const cycle of cycles) {
      const item = document.createElement("article");
      item.className = "card";
      const changes = Array.isArray(cycle.changes) ? cycle.changes as Array<Record<string, unknown>> : [];
      const completedRaw = text(cycle.completed_at, "");
      const completed = completedRaw && !Number.isNaN(Date.parse(completedRaw))
        ? new Date(completedRaw).toLocaleString()
        : "time unavailable";
      const rows = changes.map((change) => {
        const path = text(change.path, "");
        const operation = text(change.operation, "changed");
        return `<div class="recovery-path-choice"><span class="pill">${escapeHtml(operation)}</span><span class="path">${escapeHtml(path)}</span></div>`;
      }).join("");
      const changeCount = Number(cycle.change_count ?? changes.length);
      const counts = (cycle.change_counts ?? {}) as Record<string, unknown>;
      const summary = ["added", "modified", "removed"]
        .map((kind) => Number(counts[kind] ?? 0) > 0 ? `${Number(counts[kind]).toLocaleString()} ${kind}` : "")
        .filter(Boolean)
        .join(" · ") || `${changeCount.toLocaleString()} changed`;
      const snapshotAvailable = cycle.snapshot_available === true;
      const folderId = text(cycle.folder_id, "");
      const generationId = text(cycle.generation_id, "");
      item.innerHTML = `
        <div class="section-title"><h3>${escapeHtml(text(cycle.folder_label, folderId || "Folder"))}</h3><span class="pill">${escapeHtml(completed)}</span></div>
        <p><strong>${escapeHtml(summary)}</strong></p>
        <p class="reason">${snapshotAvailable ? `${Number(cycle.snapshot_entry_count ?? 0).toLocaleString()} files and folders recorded in this complete snapshot.` : "Change record only — this cycle predates complete snapshot manifests."}</p>
        <div class="actions left">
          ${snapshotAvailable ? `<button type="button" class="primary" data-action="stage-recovery-snapshot" data-folder="${escapeHtml(folderId)}" data-generation="${escapeHtml(generationId)}">Stage Complete Folder</button>` : '<button type="button" class="secondary" disabled>Full snapshot unavailable</button>'}
        </div>
        <details><summary>Show ${changeCount.toLocaleString()} changed path${changeCount === 1 ? "" : "s"}</summary><div class="recovery-path-list">${rows || '<p class="empty">No changed paths recorded.</p>'}</div>${cycle.paths_truncated ? `<p class="reason">Showing the first ${changes.length.toLocaleString()} paths.</p>` : ""}</details>`;
      recoveryRecentList.append(item);
    }
    if (cycles.length === 0) {
      recoveryRecentList.innerHTML = '<p class="empty">No successful backup cycles are available yet for this selection.</p>';
    }
    recoveryRecentLoaded = true;
  } catch (error) {
    recoveryRecentList.innerHTML = `<p class="empty">Could not load recent backup changes: ${escapeHtml(String(error))}</p>`;
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function stageRecoverySnapshot(button: HTMLElement): Promise<void> {
  const folder = button.dataset.folder ?? "";
  const generation = button.dataset.generation ?? "";
  if (!folder || !generation) return;
  setBusy("history");
  try {
    if (!(await refreshRecoveryStatus())) throw new Error("Pause backup before staging a historical folder snapshot");
    showUiCommand(["recovery", "snapshot", folder, generation]);
    const snapshot = await invoke<Record<string, unknown>>("stage_recovery_snapshot", { folder, generation });
    latestRecoveryJob = snapshot;
    if (recoveryOutput) {
      recoveryOutput.hidden = false;
      recoveryOutput.textContent = [
        "Complete historical folder staged and verified.",
        `Snapshot: ${text(snapshot.id, "-")}`,
        `Backup time: ${text(snapshot.snapshot_at, "-")}`,
        `${Number(snapshot.entry_count ?? 0).toLocaleString()} files and folders`,
        "Nothing in the watched folder was changed. Open staging to inspect or copy what you need.",
      ].join("\n");
    }
    if (recoveryStagedActions) recoveryStagedActions.hidden = false;
    setMessage("Historical folder is ready in staging", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function openRecoveryResult(kind: "staging" | "destination"): Promise<void> {
  const paths = (latestRecoveryJob?.paths ?? {}) as Record<string, unknown>;
  const path = kind === "staging"
    ? text(latestRecoveryJob?.payload, text(paths.staging, ""))
    : text(latestRecoveryJob?.watched_folder, text(latestRecoveryJob?.destination, ""));
  if (!path) {
    setMessage("Stage a file or folder snapshot first", "error");
    return;
  }
  try {
    await invoke("open_local_folder", { path });
  } catch (error) {
    setMessage(String(error), "error");
  }
}

async function loadHistory(): Promise<void> {
  const folder = historyFolder?.value ?? "";
  const path = recoveryPath?.value.trim() ?? "";
  if (!folder || !path || !historyList) {
    setMessage("Choose a folder and enter a relative file path", "error");
    return;
  }
  setBusy("history");
  try {
    showUiCommand(["recovery", "revisions", folder, path]);
    const value = await invoke<Record<string, unknown>>("get_recovery_revisions", { folder, path });
    const entries = Array.isArray(value.entries) ? value.entries as Array<Record<string, unknown>> : [];
    historyList.innerHTML = "";
    if (entries.length > 0) {
      const item = document.createElement("article");
      item.className = "card recovery-version-list";
      item.innerHTML = `<div class="section-title"><h3>${entries.length} available version${entries.length === 1 ? "" : "s"}</h3><span class="pill">Dropbox</span></div>`;
      entries.forEach((entry, index) => {
      const revision = text(entry.rev, "");
        const row = document.createElement("button");
        row.type = "button";
        row.className = "recovery-version-row";
        row.dataset.action = "recover-history";
        row.dataset.recoveryRevision = revision;
        row.innerHTML = `<span>${index === 0 ? "Current" : `Version ${index + 1}`}</span><span>${escapeHtml(text(entry.server_modified, "unknown time"))}</span><span>${formatBytes(Number(entry.size ?? 0))}</span><span>Stage</span>`;
        item.append(row);
      });
      historyList.append(item);
    }
    if (entries.length === 0) historyList.textContent = "No Dropbox revisions are available for this path within the account retention window.";
    setMessage(`Found ${entries.length} Dropbox version${entries.length === 1 ? "" : "s"}`, "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function recoverHistory(button: HTMLElement): Promise<void> {
  const folder = historyFolder?.value ?? "";
  const path = recoveryPath?.value.trim() ?? "";
  const revision = button.closest<HTMLElement>("[data-recovery-revision]")?.dataset.recoveryRevision ?? "";
  if (!folder || !path || !revision) return;
  setBusy("history");
  try {
    const paused = await refreshRecoveryStatus();
    if (!paused) throw new Error("Pause backup for recovery before staging a revision");
    showUiCommand(["recovery", "stage", folder, path, revision]);
    const job = await invoke<Record<string, unknown>>("stage_recovery_revision", { folder, path, revision });
    latestRecoveryJob = job;
    const comparison = (job.recovery_compare ?? {}) as Record<string, unknown>;
    if (recoveryOutput) {
      recoveryOutput.hidden = false;
      recoveryOutput.textContent = [
        comparison.kind === "missing_local" ? "The selected file is absent from the watched folder. Its historical version is staged and verified." : text(comparison.summary, "Revision staged and verified."),
        text(comparison.unified_diff, ""),
        `Job: ${text(job.id, "-")}`,
        "Nothing was restored. Open staging to inspect the downloaded file.",
      ].filter(Boolean).join("\n\n");
    }
    if (recoveryStagedActions) recoveryStagedActions.hidden = false;
    setMessage("Dropbox revision staged and ready to inspect", "ok");
    jobsLoaded = false;
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}
*/

function formatBytes(value: number): string {
  if (!Number.isFinite(value) || value < 0) return "-";
  const units = ["B", "KiB", "MiB", "GiB"];
  let amount = value;
  let unit = 0;
  while (amount >= 1024 && unit < units.length - 1) {
    amount /= 1024;
    unit += 1;
  }
  return `${amount.toFixed(unit === 0 ? 0 : 1)} ${units[unit]}`;
}

function renderAuditStatus(status: AuditStatus): void {
  if (auditHealth) {
    auditHealth.textContent = text(status.health);
    auditHealth.dataset.tone = status.health === "ok" ? "ok" : "warning";
  }
  if (auditLevel) auditLevel.textContent = text(status.level);
  if (auditUsage) auditUsage.textContent = `${formatBytes(Number(status.used_local_bytes))} / ${formatBytes(Number(status.max_local_bytes))}`;
  if (auditPending) auditPending.textContent = String(Number(status.pending_cloud_segments ?? 0));
  if (auditCloudTime) auditCloudTime.textContent = text(status.replication?.last_success_at, "Not copied yet");
  if (auditGaps) {
    const gapCount = Number(status.history_gap_count ?? (Array.isArray(status.gaps) ? status.gaps.length : 0));
    auditGaps.textContent = gapCount > 0 ? `${gapCount} (history incomplete)` : "None";
  }
  const level = logLevelForm?.elements.namedItem("level") as HTMLSelectElement | null;
  if (level && ["quiet", "normal", "debug", "trace"].includes(status.level)) level.value = status.level;
}

function auditContext(event: AuditEvent): string {
  return text(
    event.correlation?.operation_id,
    text(event.correlation?.job_id, text(event.correlation?.generation_id, "Other events")),
  );
}

function renderAuditEvents(events: AuditEvent[]): void {
  if (!auditEvents) return;
  auditEvents.replaceChildren();
  if (events.length === 0) {
    auditEvents.textContent = "No events match these filters.";
    return;
  }
  const grouped = new Map<string, AuditEvent[]>();
  for (const event of events) {
    const context = auditContext(event);
    const existing = grouped.get(context) ?? [];
    existing.push(event);
    grouped.set(context, existing);
  }
  for (const [context, group] of grouped) {
    const section = document.createElement("section");
    section.className = "audit-operation";
    const heading = document.createElement("h3");
    heading.textContent = context === "Other events" ? context : `Operation ${context}`;
    section.append(heading);
    for (const event of group) {
      const item = document.createElement("article");
      item.className = "audit-event";
      item.dataset.severity = event.severity;
      const detail = text(
        event.data?.path,
        text(event.data?.reason, text(event.data?.status, text(event.data?.error, ""))),
      );
      item.innerHTML = `<div class="audit-event-head"><time>${escapeHtml(text(event.occurred_at))}</time><span class="pill">${escapeHtml(text(event.severity))}</span></div><strong>${escapeHtml(text(event.event_type))}</strong>${detail ? `<p class="path">${escapeHtml(detail)}</p>` : ""}<small>${escapeHtml(text(event.component))} · sequence ${Number(event.sequence)}</small>`;
      section.append(item);
    }
    auditEvents.append(section);
  }
}

function activityQuery(): Record<string, unknown> {
  if (!activityFilterForm) return { since: "24h", limit: 200 };
  return {
    since: inputValue(activityFilterForm, "since"),
    event_type: inputValue(activityFilterForm, "event_type"),
    folder: inputValue(activityFilterForm, "folder"),
    severity: inputValue(activityFilterForm, "severity"),
    limit: 200,
  };
}

async function loadActivity(event?: SubmitEvent): Promise<void> {
  event?.preventDefault();
  setBusy("activity");
  try {
    const [status, events] = await Promise.all([
      invoke<AuditStatus>("get_log_status"),
      invoke<AuditEvent[]>("get_activity", { request: activityQuery() }),
    ]);
    renderAuditStatus(status);
    renderAuditEvents(events);
    activityLoaded = true;
    setMessage("Audit activity loaded", "ok");
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function showRecentWarnings(): Promise<void> {
  if (activityFilterForm) {
    const since = activityFilterForm.elements.namedItem("since") as HTMLSelectElement | null;
    const severity = activityFilterForm.elements.namedItem("severity") as HTMLSelectElement | null;
    const eventType = activityFilterForm.elements.namedItem("event_type") as HTMLInputElement | null;
    const folder = activityFilterForm.elements.namedItem("folder") as HTMLInputElement | null;
    if (since) since.value = "24h";
    if (severity) severity.value = "warning";
    if (eventType) eventType.value = "";
    if (folder) folder.value = "";
  }
  await loadActivity();
}

async function changeLogLevel(event: SubmitEvent | null, forcedLevel?: string, duration?: string): Promise<void> {
  event?.preventDefault();
  const levelInput = logLevelForm?.elements.namedItem("level") as HTMLSelectElement | null;
  const level = forcedLevel ?? levelInput?.value ?? "normal";
  setBusy("activity");
  try {
    const command = ["logs", "level", level];
    if (duration) command.push("--for", duration);
    showUiCommand(command);
    await invoke("set_log_level", { request: { level, duration: duration ?? null } });
    setMessage(duration ? `${level} logging enabled for ${duration}` : `Logging level set to ${level}`, "ok");
    await loadActivity();
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function syncAuditLogs(): Promise<void> {
  setBusy("activity");
  try {
    showUiCommand(["logs", "sync"]);
    const status = await invoke<AuditStatus>("sync_audit_logs");
    renderAuditStatus(status);
    setMessage("Structured logs copied to this profile's cloud", "ok");
    await loadActivity();
  } catch (error) {
    setMessage(String(error), "error");
  } finally {
    setBusy(null);
  }
}

async function copyTransferCommand(): Promise<void> {
  const command = transferCommand?.textContent ?? "";
  if (!command.startsWith("safe-sync pull") && !command.startsWith("safe-sync receive")) {
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
    const status = await invoke<SafeSyncStatus>("get_status");
    renderStatus(status);
    if (recoveryStatusNeedsRefresh(status)) await refreshRecoveryStatus();
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
    if (recoveryStatusNeedsRefresh(latestStatus)) {
      await refreshRecoveryStatus();
    }
  } catch (error) {
    renderError(error);
  } finally {
    statusRefreshInFlight = false;
  }
}

function scheduleStatusRefresh(): void {
  if (refreshTimer !== null) window.clearTimeout(refreshTimer);
  const refreshMs = latestRecovery?.active === true || (latestStatus && ["syncing", "transferring", "dirty", "cooldown", "backoff", "recovery_paused"].includes(syncState(latestStatus)))
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

async function connectDropbox(reconnect = false): Promise<void> {
  setBusy("dropbox-connect");
  try {
    showUiCommand(reconnect ? ["connect-dropbox", "--reconnect"] : ["connect-dropbox"]);
    const result = await invoke<CommandResult>("connect_dropbox", { reconnect });
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
  const available = [...document.querySelectorAll<HTMLElement>("[data-view]")].some((view) => view.dataset.view === tab);
  if (!available || (IS_QUICK_PANEL && tab !== "status")) tab = "status";
  if (!IS_QUICK_PANEL) window.localStorage.setItem("safe-sync.active-tab", tab);
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
  if (tab === "jobs" && !jobsLoaded) void loadJobs();
  if (tab === "links") {
    if (!configLoaded) void loadConfig();
    if (!computersLoaded) void loadComputers();
    if (!linksLoaded) void loadLinks(false);
  }
  if (tab === "history") {
    if (!configLoaded) void loadConfig();
    void refreshRecoveryStatus().catch((error) => setMessage(String(error), "error"));
    void loadRecoveryDownloads();
  }
  if (tab === "activity" && !activityLoaded) void loadActivity();
}

window.addEventListener("DOMContentLoaded", () => {
  document.documentElement.dataset.ready = "true";
  document.documentElement.dataset.panel = IS_QUICK_PANEL ? "quick" : "main";
  if (helpGuide) renderUserGuide(helpGuide);
  refreshButton?.addEventListener("click", () => void refreshStatus());
  toggleButton?.addEventListener("click", () => void toggleBackend());
  backupButton?.addEventListener("click", () => void backupNow());
  logsButton?.addEventListener("click", () => void openLogs());
  for (const button of document.querySelectorAll("[data-action='open-control-panel']")) {
    button.addEventListener("click", () => void openControlPanel());
  }
  document.querySelector("[data-action='close-quick']")?.addEventListener("click", () => void closeQuickPanel());
  document.querySelector("[data-action='quit-tray']")?.addEventListener("click", () => void quitTray());
  settingsForm?.addEventListener("submit", (event) => void saveSettings(event));
  addProfileForm?.addEventListener("submit", (event) => void addProfile(event));
  addFolderForm?.addEventListener("submit", (event) => void addFolder(event));
  document.querySelector("[data-action='pick-folder']")?.addEventListener("click", () => void pickFolder());
  document.querySelector("[data-action='pick-setup-folder']")?.addEventListener("click", () => void pickSetupFolder());
  document.querySelector("[data-action='connect-dropbox']")?.addEventListener("click", () => void connectDropbox());
  document.querySelector("[data-action='reconnect-dropbox']")?.addEventListener("click", () => void connectDropbox(true));
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
  computerList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "use-remote-backup") useRemoteBackup(target);
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
  jobList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "show-job") showJob(target);
    if (target?.dataset.action === "open-job-staging") void openJobFolder(target, "staging");
    if (target?.dataset.action === "open-job-destination") void openJobFolder(target, "destination");
    if (target?.dataset.action === "apply-job") void runJobAction(target, "apply");
    if (target?.dataset.action === "reconcile-job") void runJobAction(target, "reconcile");
    if (target?.dataset.action === "rollback-job") void runJobAction(target, "rollback");
  });
  linkList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    if (target?.dataset.action === "remove-link") void removeLink(target);
    if (target?.dataset.action === "review-link") void reviewLink(target);
  });
  addLinkForm?.addEventListener("submit", (event) => void addLink(event));
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
  document.querySelector("[data-action='load-jobs']")?.addEventListener("click", () => void loadJobs());
  document.querySelector("[data-action='load-link-status']")?.addEventListener("click", () => void loadLinks(true));
  document.querySelector("[data-action='load-recovery-downloads']")?.addEventListener("click", () => void loadRecoveryDownloads());
  document.querySelector("[data-action='remove-all-recovery-downloads']")?.addEventListener("click", () => void removeRecoveryDownload(null, true));
  recoveryDownloadSort?.addEventListener("change", () => renderRecoveryDownloads(latestRecoveryDownloads));
  recoveryDownloadList?.addEventListener("click", (event) => {
    const target = event.target as HTMLElement | null;
    const openButton = target?.closest<HTMLElement>("[data-action='open-recovery-download']");
    const removeButton = target?.closest<HTMLElement>("[data-action='remove-recovery-download']");
    if (openButton) void openRecoveryDownload(openButton);
    if (removeButton) void removeRecoveryDownload(removeButton);
  });
  document.querySelector("[data-action='enter-recovery']")?.addEventListener("click", () => void controlRecovery("enter"));
  document.querySelector("[data-action='clear-legacy-recovery']")?.addEventListener("click", () => void controlRecovery("clear-legacy"));
  for (const button of document.querySelectorAll("[data-action='cancel-recovery']")) {
    button.addEventListener("click", () => void controlRecovery("cancel"));
  }
  for (const button of document.querySelectorAll("[data-action='save-recovery-remote-copy']")) {
    button.addEventListener("click", () => void controlRecovery("save-remote-copy"));
  }
  for (const button of document.querySelectorAll("[data-action='open-recovery-remote-copy']")) {
    button.addEventListener("click", () => void openCancelRemoteCopy());
  }
  document.querySelector("[data-action='mark-recovery-rewound']")?.addEventListener("click", () => void controlRecovery("mark-rewound"));
  document.querySelector("[data-action='export-recovery']")?.addEventListener("click", () => void controlRecovery("export"));
  document.querySelector("[data-action='open-recovery-export']")?.addEventListener("click", () => void openRecoveryExport());
  document.querySelector("[data-action='mark-recovery-undo']")?.addEventListener("click", () => void controlRecovery("mark-undo-complete"));
  document.querySelector("[data-action='verify-recovery']")?.addEventListener("click", () => void controlRecovery("verify"));
  document.querySelector("[data-action='exit-recovery']")?.addEventListener("click", () => void controlRecovery("exit"));
  for (const button of document.querySelectorAll("[data-action='open-recovery-dropbox']")) {
    button.addEventListener("click", () => void openRecoveryDropbox());
  }
  historyFolder?.addEventListener("change", renderRestoreFolder);
  activityFilterForm?.addEventListener("submit", (event) => void loadActivity(event));
  logLevelForm?.addEventListener("submit", (event) => void changeLogLevel(event));
  document.querySelector("[data-action='load-activity']")?.addEventListener("click", () => void loadActivity());
  document.querySelector("[data-action='show-recent-warnings']")?.addEventListener("click", () => void showRecentWarnings());
  document.querySelector("[data-action='debug-two-hours']")?.addEventListener("click", () => void changeLogLevel(null, "debug", "2h"));
  document.querySelector("[data-action='sync-audit-logs']")?.addEventListener("click", () => void syncAuditLogs());
  for (const button of document.querySelectorAll<HTMLButtonElement>("[data-tab]")) {
    button.addEventListener("click", () => activateTab(button.dataset.tab ?? "status"));
  }
  activateTab(IS_QUICK_PANEL ? "status" : window.localStorage.getItem("safe-sync.active-tab") ?? "status");
  void refreshStatus();
  if (!IS_QUICK_PANEL) void loadConfig();
});
