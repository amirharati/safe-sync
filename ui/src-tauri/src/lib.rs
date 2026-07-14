use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::env;
use std::path::PathBuf;
use std::process::Command;
use std::sync::Mutex;
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
}

fn safe_sync_binary() -> String {
    if let Ok(path) = env::var("SAFE_SYNC_BIN") {
        if !path.trim().is_empty() {
            return path;
        }
    }

    let repo_bin = PathBuf::from(env!("CARGO_MANIFEST_DIR"))
        .parent()
        .and_then(|ui_dir| ui_dir.parent())
        .map(|repo_root| repo_root.join("bin/safe-sync"));

    if let Some(path) = repo_bin {
        if path.exists() {
            return path.to_string_lossy().into_owned();
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

fn refresh_tray_label(status_item: &MenuItem<Wry>) -> SafeSyncStatus {
    match read_status() {
        Ok(status) => {
            let _ = status_item.set_text(status_label(&status));
            status
        }
        Err(err) => {
            let _ = status_item.set_text("Safe Sync: Error");
            error_status(err)
        }
    }
}

fn update_menu_state(state: &State<AppState>, status: &SafeSyncStatus) {
    if let Ok(guard) = state.status_item.lock() {
        if let Some(item) = guard.as_ref() {
            let _ = item.set_text(status_label(status));
        }
    }
    if let Ok(guard) = state.toggle_item.lock() {
        if let Some(item) = guard.as_ref() {
            let _ = item.set_text(toggle_label(status));
            let _ = item.set_enabled(status.service_state != "unknown");
        }
    }
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

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .manage(AppState {
            status_item: Mutex::new(None),
            toggle_item: Mutex::new(None),
        })
        .plugin(tauri_plugin_opener::init())
        .invoke_handler(tauri::generate_handler![get_status, control_backend])
        .setup(|app| {
            let status = MenuItem::with_id(app, "status", "Safe Sync: Checking", false, None::<&str>)?;
            let show = MenuItem::with_id(app, "show", "Show Status Window", true, None::<&str>)?;
            let toggle = MenuItem::with_id(app, "backend-toggle", "Start Backend", true, None::<&str>)?;
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
                    &separator,
                    &toggle,
                    &separator,
                    &quit,
                ],
            )?;
            let icon = app
                .default_window_icon()
                .cloned()
                .expect("Safe Sync tray icon missing");

            let initial_status = refresh_tray_label(&status);
            let _ = toggle.set_text(toggle_label(&initial_status));
            if let Ok(mut guard) = app.state::<AppState>().status_item.lock() {
                guard.replace(status.clone());
            }
            if let Ok(mut guard) = app.state::<AppState>().toggle_item.lock() {
                guard.replace(toggle.clone());
            }
            let status_item = status.clone();
            let toggle_item = toggle.clone();

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
                        let status = refresh_tray_label(&status_item);
                        let _ = toggle_item.set_text(toggle_label(&status));
                    }
                    "backend-toggle" => {
                        let before = refresh_tray_label(&status_item);
                        let action = if should_stop_backend(&before) { "stop" } else { "start" };
                        if run_safe_sync(&[action]).is_err() {
                            let _ = status_item.set_text(if action == "stop" {
                                "Safe Sync: Stop failed"
                            } else {
                                "Safe Sync: Start failed"
                            });
                        }
                        let after = refresh_tray_label(&status_item);
                        let _ = toggle_item.set_text(toggle_label(&after));
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
