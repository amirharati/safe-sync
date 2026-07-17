use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::env;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Mutex;
use std::thread;
use std::time::{Duration, Instant};
use tauri::image::Image;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Manager, WindowEvent, Wry};

#[cfg(target_os = "macos")]
use objc2_app_kit::NSWindow;
#[cfg(target_os = "macos")]
use objc2_foundation::{MainThreadMarker, NSPoint, NSRect};

#[derive(Debug, Deserialize, Serialize)]
struct SafeSyncStatus {
    health: String,
    health_reason: String,
    service_state: String,
    sync_state: Value,
    daemon_seen_at: Option<String>,
    log: Option<String>,
}

#[derive(Debug, Deserialize, Serialize)]
struct SafeSyncConfigView {
    config_path: String,
    profile_id: Option<String>,
    profile_label: Option<String>,
    active_profile_id: Option<String>,
    machine_id: Option<String>,
    machine_label: Option<String>,
    remote_base: Option<String>,
    poll_interval_seconds: u64,
    debounce_seconds: u64,
    min_interval_seconds: u64,
    fallback_interval_seconds: u64,
    rate_limit_backoff_seconds: u64,
    folders: Vec<Value>,
    profiles: Vec<Value>,
}

#[derive(Debug, Deserialize)]
struct SafeSyncSettingsUpdate {
    machine_label: Option<String>,
    profile_label: Option<String>,
    remote_base: Option<String>,
    poll_interval_seconds: u64,
    debounce_seconds: u64,
    min_interval_seconds: u64,
    fallback_interval_seconds: u64,
    rate_limit_backoff_seconds: u64,
}

#[derive(Debug, Deserialize)]
struct AddFolderRequest {
    local_path: String,
    label: Option<String>,
    remote_path: Option<String>,
    trash_path: Option<String>,
    disabled: bool,
}

#[derive(Debug, Deserialize)]
struct UpdateFolderRequest {
    id: String,
    local_path: String,
    label: Option<String>,
    enabled: bool,
}

#[derive(Debug, Deserialize)]
struct RemoveFolderRequest {
    id: String,
}

#[derive(Debug, Deserialize)]
struct AddProfileRequest {
    name: String,
    remote_base: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ActivateProfileRequest {
    id: String,
}

#[derive(Debug, Serialize)]
struct CommandResult {
    ok: bool,
    output: String,
}

#[derive(Debug, Deserialize)]
struct OpenDropboxRequest {
    #[serde(alias = "remoteRoot")]
    remote_root: String,
}

fn safe_id(value: &str) -> String {
    let mut cleaned = String::with_capacity(value.len());
    let mut previous_dash = false;
    for ch in value.trim().chars() {
        let normalized = if ch.is_ascii_alphanumeric() || matches!(ch, '.' | '_' | '-') {
            ch.to_ascii_lowercase()
        } else {
            '-'
        };
        if normalized == '-' {
            if !previous_dash && !cleaned.is_empty() {
                cleaned.push('-');
            }
            previous_dash = true;
        } else {
            cleaned.push(normalized);
            previous_dash = false;
        }
    }
    while cleaned.ends_with('-') {
        cleaned.pop();
    }
    if cleaned.is_empty() {
        "default".to_string()
    } else {
        cleaned
    }
}

fn path_leaf(value: &str) -> String {
    let trimmed = value.trim().trim_end_matches(['/', '\\']);
    let leaf = trimmed
        .rsplit(['/', '\\'])
        .next()
        .unwrap_or(trimmed)
        .trim();
    leaf.to_string()
}

fn dropbox_home_url(remote_root: &str) -> Result<String, String> {
    let trimmed = remote_root.trim();
    let Some(path) = trimmed.strip_prefix("dropbox:") else {
        return Err("Open in Dropbox is only available for Dropbox remotes".to_string());
    };
    let clean = path.trim_start_matches('/');
    if clean.is_empty() {
        return Err("Dropbox path is empty".to_string());
    }
    let encoded = clean
        .split('/')
        .filter(|segment| !segment.is_empty())
        .map(|segment| {
            let mut out = String::new();
            for byte in segment.as_bytes() {
                let ch = *byte as char;
                if ch.is_ascii_alphanumeric() || matches!(ch, '-' | '_' | '.' | '~') {
                    out.push(ch);
                } else {
                    out.push_str(&format!("%{:02X}", byte));
                }
            }
            out
        })
        .collect::<Vec<_>>()
        .join("/");
    Ok(format!("https://www.dropbox.com/home/{encoded}"))
}

struct AppState {
    status_item: Mutex<Option<MenuItem<Wry>>>,
    toggle_item: Mutex<Option<MenuItem<Wry>>>,
    backup_item: Mutex<Option<MenuItem<Wry>>>,
    logs_item: Mutex<Option<MenuItem<Wry>>>,
    last_tray_click: Mutex<Option<Instant>>,
    last_stale_heal_attempt: Mutex<Option<Instant>>,
}


fn bounded_seconds(name: &str, value: u64, min: u64, max: u64) -> Result<u64, String> {
    if value < min || value > max {
        Err(format!("{name} must be between {min} and {max} seconds"))
    } else {
        Ok(value)
    }
}

fn safe_sync_binary() -> String {
    if let Ok(path) = env::var("SAFE_SYNC_BIN") {
        if !path.trim().is_empty() {
            return path;
        }
    }

    if cfg!(debug_assertions) {
        let repo_bin = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|ui_dir| ui_dir.parent())
            .map(|repo_root| repo_root.join("bin/safe-sync"));

        if let Some(path) = repo_bin {
            if path.exists() {
                return path.to_string_lossy().into_owned();
            }
        }
    }

    if let Ok(home) = env::var("HOME") {
        let home_bin = PathBuf::from(home).join(".local/bin/safe-sync");
        if home_bin.exists() {
            return home_bin.to_string_lossy().into_owned();
        }
    }

    for path in ["/usr/local/bin/safe-sync", "/opt/homebrew/bin/safe-sync"] {
        if PathBuf::from(path).exists() {
            return path.to_string();
        }
    }

    "safe-sync".to_string()
}

fn run_safe_sync(args: &[&str]) -> Result<String, String> {
    let output = Command::new(safe_sync_binary())
        .args(args)
        .output()
        .map_err(|err| format!("failed to run safe-sync: {err}"))?;

    let stdout = String::from_utf8_lossy(&output.stdout).trim().to_string();
    let stderr = String::from_utf8_lossy(&output.stderr).trim().to_string();

    if output.status.success() {
        Ok(stdout)
    } else if stderr.is_empty() {
        Err(format!("safe-sync {} exited with {}", args.join(" "), output.status))
    } else {
        Err(stderr)
    }
}

fn read_status() -> Result<SafeSyncStatus, String> {
    let stdout = run_safe_sync(&["status"])?;
    serde_json::from_str(&stdout).map_err(|err| format!("safe-sync status returned invalid JSON: {err}"))
}

async fn run_safe_sync_blocking(args: Vec<String>) -> Result<String, String> {
    tauri::async_runtime::spawn_blocking(move || {
        let refs: Vec<&str> = args.iter().map(String::as_str).collect();
        run_safe_sync(&refs)
    })
    .await
    .unwrap_or_else(|err| Err(format!("safe-sync task failed: {err}")))
}

async fn read_status_blocking() -> Result<SafeSyncStatus, String> {
    tauri::async_runtime::spawn_blocking(read_status)
        .await
        .unwrap_or_else(|err| Err(format!("status task failed: {err}")))
}

fn should_attempt_stale_heal(app: &AppHandle<Wry>, status: &SafeSyncStatus) -> bool {
    if status.health != "stale" || status.service_state != "running" {
        return false;
    }
    let state = app.state::<AppState>();
    let Ok(mut last_attempt) = state.last_stale_heal_attempt.lock() else {
        return false;
    };
    let now = Instant::now();
    if last_attempt.is_some_and(|previous| now.duration_since(previous) < Duration::from_secs(60)) {
        return false;
    }
    last_attempt.replace(now);
    true
}

async fn read_status_with_self_heal(app: &AppHandle<Wry>) -> SafeSyncStatus {
    let status = read_status_blocking().await.unwrap_or_else(error_status);
    if !should_attempt_stale_heal(app, &status) {
        return status;
    }

    let restart_result = run_safe_sync_blocking(vec!["restart".to_string()]).await;
    thread::sleep(Duration::from_millis(600));
    match read_status_blocking().await {
        Ok(recovered) => recovered,
        Err(read_err) => {
            let mut fallback = status;
            if let Err(restart_err) = restart_result {
                fallback.health_reason = format!("{}; auto-restart failed: {}", fallback.health_reason, restart_err);
            } else {
                fallback.health_reason = format!("{}; auto-restart attempted but refresh failed: {}", fallback.health_reason, read_err);
            }
            fallback
        }
    }
}

fn sync_state(status: &SafeSyncStatus) -> Option<&str> {
    status.sync_state.get("state").and_then(Value::as_str)
}

fn should_stop_backend(status: &SafeSyncStatus) -> bool {
    status.service_state == "running"
}

fn toggle_label(status: &SafeSyncStatus) -> &'static str {
    if should_stop_backend(status) {
        "Stop Backend"
    } else {
        "Start Backend"
    }
}

fn status_label(status: &SafeSyncStatus) -> String {
    if status.health == "error" {
        return "Safe Sync: Error".to_string();
    }

    match status.service_state.as_str() {
        "running" => match sync_state(status) {
            Some("syncing") => "Safe Sync: Syncing".to_string(),
            Some("backoff") => "Safe Sync: Backoff".to_string(),
            Some("cooldown") => "Safe Sync: Cooling down".to_string(),
            Some("dirty") => "Safe Sync: Changes queued".to_string(),
            Some("watching") => "Safe Sync: Watching".to_string(),
            Some(other) => format!("Safe Sync: Running ({other})"),
            None => "Safe Sync: Running".to_string(),
        },
        "stopped" => "Safe Sync: Stopped".to_string(),
        other => format!("Safe Sync: {other}"),
    }
}

fn error_status(message: String) -> SafeSyncStatus {
    SafeSyncStatus {
        health: "error".to_string(),
        health_reason: message,
        service_state: "unknown".to_string(),
        sync_state: Value::Object(Default::default()),
        daemon_seen_at: None,
        log: None,
    }
}

fn update_items(
    status_item: &MenuItem<Wry>,
    toggle_item: &MenuItem<Wry>,
    backup_item: &MenuItem<Wry>,
    logs_item: &MenuItem<Wry>,
    status: &SafeSyncStatus,
) {
    let _ = status_item.set_text(status_label(status));
    let _ = toggle_item.set_text(toggle_label(status));
    let known = status.service_state != "unknown";
    let _ = toggle_item.set_enabled(known);
    let _ = backup_item.set_enabled(status.service_state == "running");
    let _ = logs_item.set_enabled(status.log.as_ref().is_some_and(|path| !path.is_empty()));
}

fn refresh_menu_items(
    status_item: &MenuItem<Wry>,
    toggle_item: &MenuItem<Wry>,
    backup_item: &MenuItem<Wry>,
    logs_item: &MenuItem<Wry>,
) -> SafeSyncStatus {
    match read_status() {
        Ok(status) => {
            update_items(status_item, toggle_item, backup_item, logs_item, &status);
            status
        }
        Err(err) => {
            let status = error_status(err);
            update_items(status_item, toggle_item, backup_item, logs_item, &status);
            status
        }
    }
}

fn update_menu_state_from_app(app: &AppHandle<Wry>, status: &SafeSyncStatus) {
    let state = app.state::<AppState>();
    let Ok(status_guard) = state.status_item.lock() else { return };
    let Ok(toggle_guard) = state.toggle_item.lock() else { return };
    let Ok(backup_guard) = state.backup_item.lock() else { return };
    let Ok(logs_guard) = state.logs_item.lock() else { return };
    if let (Some(status_item), Some(toggle_item), Some(backup_item), Some(logs_item)) = (
        status_guard.as_ref(),
        toggle_guard.as_ref(),
        backup_guard.as_ref(),
        logs_guard.as_ref(),
    ) {
        update_items(status_item, toggle_item, backup_item, logs_item, status);
    }
}

fn open_path(path: &str) -> Result<(), String> {
    let command = if cfg!(target_os = "macos") {
        ("open", vec![path])
    } else if cfg!(target_os = "linux") {
        ("xdg-open", vec![path])
    } else if cfg!(target_os = "windows") {
        ("cmd", vec!["/C", "start", "", path])
    } else {
        return Err("opening files is unsupported on this OS".to_string());
    };

    Command::new(command.0)
        .args(command.1)
        .spawn()
        .map_err(|err| format!("failed to open {path}: {err}"))?;
    Ok(())
}

#[tauri::command]
async fn get_config() -> Result<SafeSyncConfigView, String> {
    let stdout = run_safe_sync_blocking(vec!["config".to_string(), "show".to_string()]).await?;
    serde_json::from_str(&stdout).map_err(|err| format!("safe-sync config show returned invalid JSON: {err}"))
}

#[tauri::command]
async fn save_settings(update: SafeSyncSettingsUpdate) -> Result<SafeSyncConfigView, String> {
    let mut args = vec![
        "config".to_string(),
        "update".to_string(),
        "--poll-interval-seconds".to_string(),
        bounded_seconds("poll interval", update.poll_interval_seconds, 1, 3600)?.to_string(),
        "--debounce-seconds".to_string(),
        bounded_seconds("debounce", update.debounce_seconds, 1, 3600)?.to_string(),
        "--min-interval-seconds".to_string(),
        bounded_seconds("minimum interval", update.min_interval_seconds, 0, 86400)?.to_string(),
        "--fallback-interval-seconds".to_string(),
        bounded_seconds("fallback interval", update.fallback_interval_seconds, 60, 86400)?.to_string(),
        "--rate-limit-backoff-seconds".to_string(),
        bounded_seconds("rate limit backoff", update.rate_limit_backoff_seconds, 60, 86400)?.to_string(),
    ];
    if let Some(machine_label) = update.machine_label.filter(|value| !value.trim().is_empty()) {
        args.push("--machine-label".to_string());
        args.push(machine_label);
    }
    if let Some(profile_label) = update.profile_label.filter(|value| !value.trim().is_empty()) {
        args.push("--profile-label".to_string());
        args.push(profile_label);
    }
    if let Some(remote_base) = update.remote_base.filter(|value| !value.trim().is_empty()) {
        args.push("--remote-base".to_string());
        args.push(remote_base);
    }
    let stdout = run_safe_sync_blocking(args).await?;
    serde_json::from_str(&stdout).map_err(|err| format!("safe-sync config update returned invalid JSON: {err}"))
}

#[tauri::command]
async fn add_folder(request: AddFolderRequest) -> Result<SafeSyncConfigView, String> {
    let local_path = request.local_path.trim();
    if local_path.is_empty() {
        return Err("folder path is required".to_string());
    }
    let folder_name = path_leaf(local_path);
    let folder_id = safe_id(&folder_name);
    let mut args = vec![
        "folders".to_string(),
        "add".to_string(),
        folder_id,
        local_path.to_string(),
    ];
    if let Some(label) = request.label.filter(|value| !value.trim().is_empty()) {
        args.push("--label".to_string());
        args.push(label);
    }
    if let Some(remote_path) = request.remote_path.filter(|value| !value.trim().is_empty()) {
        args.push("--remote-path".to_string());
        args.push(remote_path);
    }
    if let Some(trash_path) = request.trash_path.filter(|value| !value.trim().is_empty()) {
        args.push("--trash-path".to_string());
        args.push(trash_path);
    }
    if request.disabled {
        args.push("--disabled".to_string());
    }
    run_safe_sync_blocking(args).await?;
    get_config().await
}

#[tauri::command]
async fn update_folder(request: UpdateFolderRequest) -> Result<SafeSyncConfigView, String> {
    if request.id.trim().is_empty() || request.local_path.trim().is_empty() {
        return Err("folder id and local path are required".to_string());
    }
    let mut args = vec![
        "folders".to_string(),
        "update".to_string(),
        request.id,
        request.local_path,
    ];
    if let Some(label) = request.label.filter(|value| !value.trim().is_empty()) {
        args.push("--label".to_string());
        args.push(label);
    }
    args.push(if request.enabled { "--enabled" } else { "--disabled" }.to_string());
    run_safe_sync_blocking(args).await?;
    get_config().await
}

#[tauri::command]
async fn remove_folder(request: RemoveFolderRequest) -> Result<SafeSyncConfigView, String> {
    if request.id.trim().is_empty() {
        return Err("folder id is required".to_string());
    }
    run_safe_sync_blocking(vec!["folders".to_string(), "remove".to_string(), request.id]).await?;
    get_config().await
}

#[tauri::command]
async fn add_profile(request: AddProfileRequest) -> Result<SafeSyncConfigView, String> {
    let name = request.name.trim();
    if name.is_empty() {
        return Err("profile name is required".to_string());
    }
    let profile_id = safe_id(name);
    let mut args = vec!["profiles".to_string(), "add".to_string(), profile_id.clone()];
    args.push("--label".to_string());
    args.push(name.to_string());
    args.push("--machine-id".to_string());
    args.push(profile_id);
    args.push("--machine-label".to_string());
    args.push(name.to_string());
    if let Some(remote_base) = request.remote_base.filter(|value| !value.trim().is_empty()) {
        args.push("--remote-base".to_string());
        args.push(remote_base);
    }
    run_safe_sync_blocking(args).await?;
    get_config().await
}

#[tauri::command]
async fn activate_profile(request: ActivateProfileRequest, app: AppHandle<Wry>) -> Result<SafeSyncConfigView, String> {
    if request.id.trim().is_empty() {
        return Err("profile id is required".to_string());
    }
    run_safe_sync_blocking(vec!["profiles".to_string(), "activate".to_string(), request.id]).await?;
    let status = read_status_with_self_heal(&app).await;
    update_menu_state_from_app(&app, &status);
    get_config().await
}

#[tauri::command]
async fn get_computers() -> Result<Value, String> {
    let stdout = run_safe_sync_blocking(vec!["computers".to_string()]).await?;
    serde_json::from_str(&stdout).map_err(|err| format!("safe-sync computers returned invalid JSON: {err}"))
}

#[tauri::command]
async fn list_remote(target: String, depth: u64) -> Result<CommandResult, String> {
    if target.trim().is_empty() {
        return Err("remote target is required".to_string());
    }
    let depth = depth.clamp(1, 5).to_string();
    let output = run_safe_sync_blocking(vec!["list".to_string(), target, "--depth".to_string(), depth]).await?;
    Ok(CommandResult { ok: true, output })
}

#[tauri::command]
async fn pull_remote(source: String, destination: String, dry_run: bool) -> Result<CommandResult, String> {
    if source.trim().is_empty() || destination.trim().is_empty() {
        return Err("source and destination are required".to_string());
    }
    let args = if dry_run {
        vec!["pull".to_string(), source, destination, "--dry-run".to_string()]
    } else {
        vec!["pull".to_string(), source, destination]
    };
    let output = run_safe_sync_blocking(args).await?;
    Ok(CommandResult { ok: true, output })
}

#[tauri::command]
async fn get_status(app: AppHandle<Wry>) -> Result<SafeSyncStatus, String> {
    let status = read_status_with_self_heal(&app).await;
    update_menu_state_from_app(&app, &status);
    Ok(status)
}

#[tauri::command]
async fn control_backend(action: String, app: AppHandle<Wry>) -> Result<SafeSyncStatus, String> {
    match action.as_str() {
        "start" | "stop" | "restart" => {
            run_safe_sync_blocking(vec![action]).await?;
            let status = read_status_with_self_heal(&app).await;
            update_menu_state_from_app(&app, &status);
            Ok(status)
        }
        _ => Err(format!("unknown backend action: {action}")),
    }
}

#[tauri::command]
async fn backup_now(app: AppHandle<Wry>) -> Result<SafeSyncStatus, String> {
    if let Ok(status) = read_status_blocking().await {
        if status.service_state != "running" {
            update_menu_state_from_app(&app, &status);
            return Err("Backend daemon is stopped; start it before running Backup Now".to_string());
        }
    }

    let result = run_safe_sync_blocking(vec!["backup".to_string()]).await;
    let status = read_status_with_self_heal(&app).await;
    let status = if status.health == "error" && status.health_reason.starts_with("status task failed:") {
        error_status(match result {
            Ok(_) => status.health_reason,
            Err(command_err) => format!("{command_err}; additionally failed to refresh status: {}", status.health_reason),
        })
    } else {
        status
    };
    update_menu_state_from_app(&app, &status);
    Ok(status)
}

fn show_control_panel_window(app: &AppHandle<Wry>) {
    hide_quick_panel(app);
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg(target_os = "macos")]
fn tray_anchor(app: &AppHandle<Wry>) -> Option<(NSRect, NSRect)> {
    let tray = app.tray_by_id("main")?;
    tray.with_inner_tray_icon(|inner| {
        let mtm = MainThreadMarker::new()?;
        let status_item = inner.ns_status_item()?;
        let button = status_item.button(mtm)?;
        let status_window = button.window()?;
        let screen = status_window.screen()?;
        Some((status_window.frame(), screen.visibleFrame()))
    })
    .ok()
    .flatten()
}

#[cfg(target_os = "macos")]
fn native_quick_window(app: &AppHandle<Wry>) -> Option<&NSWindow> {
    let window = app.get_webview_window("quick")?;
    let pointer = window.ns_window().ok()?;
    unsafe { pointer.cast::<NSWindow>().as_ref() }
}

#[cfg(target_os = "macos")]
fn hide_quick_panel(app: &AppHandle<Wry>) {
    if let Some(window) = native_quick_window(app) {
        window.orderOut(None);
    }
}

#[cfg(not(target_os = "macos"))]
fn hide_quick_panel(app: &AppHandle<Wry>) {
    if let Some(window) = app.get_webview_window("quick") {
        let _ = window.hide();
    }
}

#[cfg(target_os = "macos")]
fn show_quick_panel_fallback(app: &AppHandle<Wry>) {
    if let Some(window) = app.get_webview_window("quick") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

#[cfg(target_os = "macos")]
fn toggle_quick_panel(app: &AppHandle<Wry>) {
    let Some(window) = native_quick_window(app) else {
        show_quick_panel_fallback(app);
        return;
    };
    if window.isVisible() {
        window.orderOut(None);
        return;
    }

    let Some((anchor, screen)) = tray_anchor(app) else {
        show_quick_panel_fallback(app);
        return;
    };
    let panel = window.frame();
    let centered_x = anchor.origin.x + (anchor.size.width - panel.size.width) / 2.0;
    let x = centered_x.clamp(screen.origin.x, screen.origin.x + screen.size.width - panel.size.width);
    let y = (anchor.origin.y - panel.size.height - 6.0)
        .clamp(screen.origin.y, screen.origin.y + screen.size.height - panel.size.height);

    window.setFrameOrigin(NSPoint::new(x, y));
    window.makeKeyAndOrderFront(None);
    show_quick_panel_fallback(app);
}

#[cfg(not(target_os = "macos"))]
fn toggle_quick_panel(app: &AppHandle<Wry>) {
    let Some(window) = app.get_webview_window("quick") else {
        return;
    };
    if window.is_visible().unwrap_or(false) {
        let _ = window.hide();
    } else {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn accept_tray_click(app: &AppHandle<Wry>) -> bool {
    let state = app.state::<AppState>();
    let Ok(mut last_click) = state.last_tray_click.lock() else {
        return true;
    };
    let now = Instant::now();
    if last_click.is_some_and(|previous| now.duration_since(previous) < Duration::from_millis(40)) {
        return false;
    }
    last_click.replace(now);
    true
}

#[tauri::command]
fn open_logs(_app: AppHandle<Wry>) -> Result<(), String> {
    let status = read_status()?;
    let log = status.log.ok_or_else(|| "safe-sync status did not include a log path".to_string())?;
    open_path(&log)
}

#[tauri::command]
fn open_control_panel(app: AppHandle<Wry>) {
    show_control_panel_window(&app);
}

#[tauri::command]
fn open_dropbox_location(request: OpenDropboxRequest) -> Result<(), String> {
    let url = dropbox_home_url(&request.remote_root)?;
    open_path(&url)
}

#[tauri::command]
fn open_local_folder(path: String) -> Result<(), String> {
    let target = PathBuf::from(path.trim());
    if !target.is_dir() {
        return Err("local folder does not exist".to_string());
    }
    open_path(&target.to_string_lossy())
}

#[tauri::command]
fn close_quick_panel(app: AppHandle<Wry>) {
    hide_quick_panel(&app);
}

#[tauri::command]
fn quit_tray(app: AppHandle<Wry>) {
    app.exit(0);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let builder = tauri::Builder::default();

    builder
        .manage(AppState {
            status_item: Mutex::new(None),
            toggle_item: Mutex::new(None),
            backup_item: Mutex::new(None),
            logs_item: Mutex::new(None),
            last_tray_click: Mutex::new(None),
            last_stale_heal_attempt: Mutex::new(None),
        })
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![
            get_status,
            control_backend,
            backup_now,
            open_logs,
            open_control_panel,
            open_dropbox_location,
            open_local_folder,
            close_quick_panel,
            quit_tray,
            get_config,
            save_settings,
            add_folder,
            update_folder,
            remove_folder,
            add_profile,
            activate_profile,
            get_computers,
            list_remote,
            pull_remote
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                api.prevent_close();
                let _ = window.hide();
            }
        })
        .setup(|app| {
            let status = MenuItem::with_id(app, "status", "Safe Sync: Checking", false, None::<&str>)?;
            let show = MenuItem::with_id(app, "show", "Open Control Panel", true, None::<&str>)?;
            let toggle = MenuItem::with_id(app, "backend-toggle", "Start Backend", true, None::<&str>)?;
            let backup = MenuItem::with_id(app, "backup-now", "Backup Now", true, None::<&str>)?;
            let logs = MenuItem::with_id(app, "open-logs", "Open Logs", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Tray", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(
                app,
                &[
                    &status,
                    &separator,
                    &show,
                    &logs,
                    &separator,
                    &quit,
                ],
            )?;
            let icon = Image::from_bytes(include_bytes!("../icons/tray-icon.png"))?;

            refresh_menu_items(&status, &toggle, &backup, &logs);
            if let Ok(mut guard) = app.state::<AppState>().status_item.lock() {
                guard.replace(status.clone());
            }
            if let Ok(mut guard) = app.state::<AppState>().toggle_item.lock() {
                guard.replace(toggle.clone());
            }
            if let Ok(mut guard) = app.state::<AppState>().backup_item.lock() {
                guard.replace(backup.clone());
            }
            if let Ok(mut guard) = app.state::<AppState>().logs_item.lock() {
                guard.replace(logs.clone());
            }
            let status_item = status.clone();
            let toggle_item = toggle.clone();
            let backup_item = backup.clone();
            let logs_item = logs.clone();

            thread::spawn({
                let status_item = status.clone();
                let toggle_item = toggle.clone();
                let backup_item = backup.clone();
                let logs_item = logs.clone();
                move || loop {
                    thread::sleep(Duration::from_secs(10));
                    refresh_menu_items(&status_item, &toggle_item, &backup_item, &logs_item);
                }
            });

            TrayIconBuilder::with_id("main")
                .tooltip("Safe Sync")
                .icon(icon)
                .menu(&menu)
                .show_menu_on_left_click(false)
                .on_tray_icon_event(|tray, event| {
                    if let TrayIconEvent::Click {
                        button: MouseButton::Left,
                        button_state: MouseButtonState::Down,
                        ..
                    } = event
                    {
                        if accept_tray_click(tray.app_handle()) {
                            toggle_quick_panel(tray.app_handle());
                        }
                    }
                })
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "show" => {
                        show_control_panel_window(app);
                    }
                    "open-logs" => {
                        let status_item = status_item.clone();
                        let toggle_item = toggle_item.clone();
                        let backup_item = backup_item.clone();
                        let logs_item = logs_item.clone();
                        thread::spawn(move || {
                            if let Ok(status) = read_status() {
                                if let Some(log) = status.log {
                                    let _ = open_path(&log);
                                }
                            }
                            refresh_menu_items(&status_item, &toggle_item, &backup_item, &logs_item);
                        });
                    }
                    "quit" => {
                        app.exit(0);
                    }
                    _ => {}
                })
                .build(app)?;

            Ok(())
        })
        .run(tauri::generate_context!())
        .expect("error while running Safe Sync tray UI");
}
