use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::env;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Mutex;
use std::thread;
use std::time::Duration;
use tauri::menu::{Menu, MenuItem, PredefinedMenuItem};
use tauri::tray::TrayIconBuilder;
use tauri::{Manager, State, Wry};

#[derive(Debug, Deserialize, Serialize)]
struct SafeSyncStatus {
    health: String,
    health_reason: String,
    service_state: String,
    sync_state: Value,
    daemon_seen_at: Option<String>,
    log: Option<String>,
}

struct AppState {
    status_item: Mutex<Option<MenuItem<Wry>>>,
    toggle_item: Mutex<Option<MenuItem<Wry>>>,
    backup_item: Mutex<Option<MenuItem<Wry>>>,
    logs_item: Mutex<Option<MenuItem<Wry>>>,
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
    let _ = backup_item.set_enabled(known);
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

fn update_menu_state(state: &State<AppState>, status: &SafeSyncStatus) {
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
fn get_status(state: State<AppState>) -> SafeSyncStatus {
    let status = read_status().unwrap_or_else(error_status);
    update_menu_state(&state, &status);
    status
}

#[tauri::command]
fn control_backend(action: String, state: State<AppState>) -> Result<SafeSyncStatus, String> {
    match action.as_str() {
        "start" | "stop" | "restart" => {
            run_safe_sync(&[action.as_str()])?;
            let status = read_status().unwrap_or_else(error_status);
            update_menu_state(&state, &status);
            Ok(status)
        }
        _ => Err(format!("unknown backend action: {action}")),
    }
}

#[tauri::command]
fn backup_now(state: State<AppState>) -> SafeSyncStatus {
    let result = run_safe_sync(&["backup"]);
    let status = read_status().unwrap_or_else(|status_err| {
        error_status(match result {
            Ok(_) => status_err,
            Err(command_err) => format!("{command_err}; additionally failed to refresh status: {status_err}"),
        })
    });
    update_menu_state(&state, &status);
    status
}

#[tauri::command]
fn open_logs() -> Result<(), String> {
    let status = read_status()?;
    let log = status.log.ok_or_else(|| "safe-sync status did not include a log path".to_string())?;
    open_path(&log)
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AppState {
            status_item: Mutex::new(None),
            toggle_item: Mutex::new(None),
            backup_item: Mutex::new(None),
            logs_item: Mutex::new(None),
        })
        .plugin(tauri_plugin_single_instance::init(|app, _args, _cwd| {
            if let Some(window) = app.get_webview_window("main") {
                let _ = window.show();
                let _ = window.set_focus();
            }
        }))
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![get_status, control_backend, backup_now, open_logs])
        .setup(|app| {
            let status = MenuItem::with_id(app, "status", "Safe Sync: Checking", false, None::<&str>)?;
            let show = MenuItem::with_id(app, "show", "Show Status Window", true, None::<&str>)?;
            let toggle = MenuItem::with_id(app, "backend-toggle", "Start Backend", true, None::<&str>)?;
            let backup = MenuItem::with_id(app, "backup-now", "Backup Now", true, None::<&str>)?;
            let logs = MenuItem::with_id(app, "open-logs", "Open Logs", true, None::<&str>)?;
            let refresh = MenuItem::with_id(app, "refresh", "Refresh Status", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit Tray", true, None::<&str>)?;
            let separator = PredefinedMenuItem::separator(app)?;
            let menu = Menu::with_items(
                app,
                &[
                    &status,
                    &separator,
                    &show,
                    &refresh,
                    &logs,
                    &separator,
                    &toggle,
                    &backup,
                    &separator,
                    &quit,
                ],
            )?;
            let icon = app
                .default_window_icon()
                .cloned()
                .expect("Safe Sync tray icon missing");

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

            TrayIconBuilder::new()
                .tooltip("Safe Sync")
                .icon(icon)
                .menu(&menu)
                .show_menu_on_left_click(true)
                .on_menu_event(move |app, event| match event.id().as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            let _ = window.show();
                            let _ = window.set_focus();
                        }
                    }
                    "refresh" => {
                        refresh_menu_items(&status_item, &toggle_item, &backup_item, &logs_item);
                    }
                    "open-logs" => {
                        if let Ok(status) = read_status() {
                            if let Some(log) = status.log {
                                let _ = open_path(&log);
                            }
                        }
                        refresh_menu_items(&status_item, &toggle_item, &backup_item, &logs_item);
                    }
                    "backend-toggle" => {
                        let before = refresh_menu_items(&status_item, &toggle_item, &backup_item, &logs_item);
                        let action = if should_stop_backend(&before) { "stop" } else { "start" };
                        if run_safe_sync(&[action]).is_err() {
                            let _ = status_item.set_text(if action == "stop" {
                                "Safe Sync: Stop failed"
                            } else {
                                "Safe Sync: Start failed"
                            });
                        }
                        refresh_menu_items(&status_item, &toggle_item, &backup_item, &logs_item);
                    }
                    "backup-now" => {
                        let _ = status_item.set_text("Safe Sync: Backup running");
                        let status_item = status_item.clone();
                        let toggle_item = toggle_item.clone();
                        let backup_item = backup_item.clone();
                        let logs_item = logs_item.clone();
                        thread::spawn(move || {
                            let _ = run_safe_sync(&["backup"]);
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
