//! Monitoring Anything desktop app: Tauri commands for the workspace, the Python
//! collector, the floating widget and the system tray.

mod agent;
mod collector;
mod fsutil;
mod fswatch;
mod settings;
mod skill;
mod tray;
mod windows;
mod workspace;

use serde::Serialize;
use serde_json::Value;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};
use tauri_plugin_opener::OpenerExt;

use collector::{CliOutput, CollectorState, PythonCandidate, Runner, WatchStatus};
use settings::{AppSettings, SettingsState};
use windows::NavigateTarget;
use workspace::{Snapshot, WorkspaceInfo};

const AUTOSTART_FLAG: &str = "--autostart";
/// `monitoring-anything --ma-request <file.json>`: a request from the `ma` command line (an agent), shown in the UI.
const REQUEST_FLAG: &str = "--ma-request";

#[derive(Default)]
struct PendingRequests(Mutex<Vec<Value>>);

/// Read a request file named after --ma-request (bounded, JSON object, action "connect"), then delete it.
fn request_from_args(args: &[String]) -> Option<Value> {
    let index = args.iter().position(|a| a == REQUEST_FLAG)?;
    let path = PathBuf::from(args.get(index + 1)?);
    let text = fsutil::read_bounded(&path, 4 * 1024 * 1024).ok()?;
    let _ = std::fs::remove_file(&path);
    let value: Value = serde_json::from_str(&text).ok()?;
    (value.get("action").and_then(Value::as_str) == Some("connect")).then_some(value)
}

fn deliver_request(app: &AppHandle, request: Value) {
    windows::show_main(app);
    if let Ok(mut pending) = app.state::<PendingRequests>().0.lock() {
        pending.push(request.clone());
    }
    let _ = app.emit_to(windows::MAIN, "rd://request", request);
}

/// The monitoring-anything skill for Claude Code and Codex: where it is and whether the app installed it.
#[tauri::command]
async fn skill_status(app: AppHandle) -> Result<Vec<skill::SkillTarget>> {
    blocking(move || skill::status(&app)).await
}

#[tauri::command]
async fn skill_install(app: AppHandle, agents: Vec<String>, force: bool) -> Result<Vec<skill::SkillTarget>> {
    blocking(move || skill::install(&app, &agents, force)).await
}

#[tauri::command]
async fn skill_uninstall(app: AppHandle, agent: String) -> Result<Vec<skill::SkillTarget>> {
    blocking(move || skill::uninstall(&app, &agent)).await
}

/// Requests that arrived before the page listened (the app was started by `ma connect`).
#[tauri::command]
fn take_requests(app: AppHandle) -> Vec<Value> {
    app.state::<PendingRequests>().0.lock().map(|mut pending| std::mem::take(&mut *pending)).unwrap_or_default()
}

/// Record where this app, its Python and the `ma` command line are, and write an `ma` launcher next to the settings,
/// so agents (Claude Code, Codex) can find and run it: `<config>/ma.cmd` on Windows, `<config>/ma` elsewhere.
fn write_cli_locator(app: &AppHandle) {
    let Ok(dir) = app.path().app_config_dir() else { return };
    let exe = std::env::current_exe().ok().map(|p| p.display().to_string());
    let python = collector::bundled_python(app).map(|p| p.display().to_string())
        .or_else(|| collector::detect_python().into_iter().find(|c| c.ok).map(|c| c.path));
    let cli = collector::collector_dir(app).ok().map(|d| d.join("tools").join("ma.py").display().to_string());
    let info = serde_json::json!({"schemaVersion": 1, "appExe": exe, "python": python, "cli": cli,
        "version": app.package_info().version.to_string(), "writtenAt": fsutil::utc_iso()});
    let _ = std::fs::create_dir_all(&dir);
    let _ = fsutil::atomic_write(&dir.join("cli.json"), format!("{}\n", serde_json::to_string_pretty(&info).unwrap_or_default()).as_bytes());
    let (Some(python), Some(cli)) = (python, cli) else { return };
    if cfg!(windows) {
        let script = format!("@echo off\r\nset PYTHONUTF8=1\r\n\"{python}\" -E -s -B -X utf8 \"{cli}\" %*\r\n");
        let _ = fsutil::atomic_write(&dir.join("ma.cmd"), script.as_bytes());
    } else {
        let path = dir.join("ma");
        let script = format!("#!/bin/sh\nexec \"{python}\" -E -s -B -X utf8 \"{cli}\" \"$@\"\n");
        if fsutil::atomic_write(&path, script.as_bytes()).is_ok() {
            #[cfg(unix)]
            {
                use std::os::unix::fs::PermissionsExt;
                let _ = std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o755));
            }
        }
    }
}
const BLOCKED_EXTENSIONS: [&str; 16] =
    ["exe", "bat", "cmd", "com", "msi", "ps1", "vbs", "vbe", "js", "jse", "wsf", "scr", "lnk", "jar", "sh", "app"];

type Result<T> = std::result::Result<T, String>;

async fn blocking<T: Send + 'static>(task: impl FnOnce() -> Result<T> + Send + 'static) -> Result<T> {
    tauri::async_runtime::spawn_blocking(task).await.map_err(|e| e.to_string())?
}

fn settings_state(app: &AppHandle) -> tauri::State<'_, SettingsState> {
    app.state::<SettingsState>()
}

/// The open workspace root, verified to still be a workspace.
fn workspace_root(app: &AppHandle) -> Result<PathBuf> {
    let path = settings_state(app).get().workspace.ok_or("Open or create a workspace first")?;
    let root = PathBuf::from(&path);
    if !root.join(workspace::MARKER).is_file() {
        return Err(format!("The workspace folder is unavailable: {path}"));
    }
    Ok(root)
}

/// Which interpreter runs the collector: a path chosen in Settings, else the bundled runtime, else a system Python 3.10+
/// (detected each time, never saved, so installing or removing Pythons is picked up).
#[derive(Serialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
pub(crate) struct PythonChoice {
    path: String,
    /// "custom" | "bundled" | "system"
    source: &'static str,
}

fn python_choice(app: &AppHandle) -> Result<PythonChoice> {
    if let Some(path) = settings_state(app).get().python_path {
        if !Path::new(&path).is_file() {
            return Err(format!("Python was not found at {path}. Choose another interpreter in Settings, or clear the field to use the bundled one."));
        }
        return Ok(PythonChoice { path, source: "custom" });
    }
    if let Some(path) = collector::bundled_python(app) {
        return Ok(PythonChoice { path: path.display().to_string(), source: "bundled" });
    }
    let found = collector::detect_python().into_iter().find(|c| c.ok)
        .ok_or("Python 3.10 or newer was not found. Install Python, or set its path in Settings.")?;
    Ok(PythonChoice { path: found.path, source: "system" })
}

/// Python + collector + workspace for a collector run.
pub(crate) fn runner(app: &AppHandle) -> Result<Runner> {
    let workspace = workspace_root(app)?;
    let choice = python_choice(app)?;
    let cli = collector::collector_dir(app)?.join("tools").join("cli.py");
    Ok(Runner { python: PathBuf::from(choice.path), cli, workspace, isolated: choice.source == "bundled" })
}

fn start_workspace_watch(app: &AppHandle, root: &Path) {
    if let Err(error) = app.state::<fswatch::WatchState>().watch(app, root.to_path_buf()) {
        let _ = app.emit("app://error", error);
    }
}

fn activate_workspace(app: &AppHandle, info: &WorkspaceInfo) -> Result<()> {
    let collector = app.state::<Arc<CollectorState>>().inner().clone();
    if collector.is_running() {
        collector.stop_watch(app)?;
    }
    let state = settings_state(app);
    let mut settings = state.get();
    settings.remember_workspace(&info.root);
    let saved = state.save(settings)?;
    start_workspace_watch(app, Path::new(&info.root));
    let _ = app.emit("settings://changed", saved);
    let _ = app.emit("workspace://opened", info.clone());
    Ok(())
}

// ----------------------------------------------------------------------------- settings

#[tauri::command]
fn get_settings(app: AppHandle) -> AppSettings {
    settings_state(&app).get()
}

#[tauri::command]
fn update_settings(app: AppHandle, patch: Value) -> Result<AppSettings> {
    let state = settings_state(&app);
    let next = state.get().merged(&patch)?;
    let saved = state.save(next)?;
    windows::apply_widget_settings(&app);
    let _ = app.emit("settings://changed", saved.clone());
    Ok(saved)
}

#[derive(Serialize)]
#[serde(rename_all = "camelCase")]
struct AppInfo {
    version: String,
    collector_dir: Option<String>,
    platform: String,
    settings_path: String,
    launched_by_autostart: bool,
}

#[tauri::command]
fn app_info(app: AppHandle) -> AppInfo {
    AppInfo {
        version: app.package_info().version.to_string(),
        collector_dir: collector::collector_dir(&app).ok().map(|p| p.display().to_string()),
        platform: std::env::consts::OS.to_string(),
        settings_path: settings_state(&app).path.display().to_string(),
        launched_by_autostart: std::env::args().any(|a| a == AUTOSTART_FLAG),
    }
}

// ----------------------------------------------------------------------------- workspace

#[tauri::command]
fn workspace_current(app: AppHandle) -> Option<WorkspaceInfo> {
    let path = settings_state(&app).get().workspace?;
    workspace::open(&path).ok()
}

#[tauri::command]
async fn workspace_create(app: AppHandle, path: String, name: Option<String>) -> Result<WorkspaceInfo> {
    let info = blocking(move || workspace::create(&path, name)).await?;
    activate_workspace(&app, &info)?;
    Ok(info)
}

#[tauri::command]
async fn workspace_open(app: AppHandle, path: String) -> Result<WorkspaceInfo> {
    let info = blocking(move || workspace::open(&path)).await?;
    activate_workspace(&app, &info)?;
    Ok(info)
}

#[tauri::command]
async fn workspace_snapshot(app: AppHandle) -> Result<Snapshot> {
    let root = workspace_root(&app)?;
    let builtin = collector::collector_dir(&app).ok().map(|dir| dir.join("profiles").join("builtin"));
    blocking(move || workspace::snapshot(&root, builtin.as_deref())).await
}

#[tauri::command]
async fn project_update(app: AppHandle, id: String, patch: Value) -> Result<Value> {
    let root = workspace_root(&app)?;
    blocking(move || workspace::update_record(&root, &id, &patch)).await
}

#[tauri::command]
async fn milestones_write(app: AppHandle, id: String, content: String, expected: Option<String>) -> Result<()> {
    let root = workspace_root(&app)?;
    blocking(move || workspace::write_milestones(&root, &id, &content, expected.as_deref())).await
}

/// Point a source at another folder (see workspace::remap_source). Pause live updates around it from the UI.
#[tauri::command]
async fn source_remap(app: AppHandle, source_id: String, folder: String) -> Result<Value> {
    let root = workspace_root(&app)?;
    blocking(move || workspace::remap_source(&root, &source_id, &folder)).await
}

#[tauri::command]
async fn project_remove(app: AppHandle, id: String) -> Result<String> {
    let root = workspace_root(&app)?;
    blocking(move || workspace::remove_project(&root, &id)).await
}

// ----------------------------------------------------------------------------- collector

#[tauri::command]
async fn collector_run(app: AppHandle, command: String, args: Vec<String>, input: Option<String>, timeout: Option<u64>) -> Result<CliOutput> {
    let runner = runner(&app)?;
    blocking(move || collector::run(&runner, &command, &args, input.as_deref(), timeout)).await
}

// ----------------------------------------------------------------------------- agent

/// Whether a key is stored for the provider (the key itself never leaves Rust).
#[tauri::command]
async fn agent_key_status(provider_id: String) -> Result<bool> {
    blocking(move || agent::has_key(&provider_id)).await
}

#[tauri::command]
async fn agent_key_set(provider_id: String, value: Option<String>) -> Result<bool> {
    blocking(move || agent::set_key(&provider_id, value.as_deref())).await
}

/// `plan propose` with the stored key added; cancellable with `agent_cancel(run_id)`.
#[tauri::command]
async fn agent_propose(app: AppHandle, run_id: String, request: Value) -> Result<CliOutput> {
    let runner = runner(&app)?;
    let state = app.state::<Arc<agent::AgentState>>().inner().clone();
    blocking(move || state.run_action(&runner, "propose", &run_id, &request)).await
}

/// `plan test` (connection check) with the stored key added; cancellable like a proposal.
#[tauri::command]
async fn agent_test(app: AppHandle, run_id: String, request: Value) -> Result<CliOutput> {
    let runner = runner(&app)?;
    let state = app.state::<Arc<agent::AgentState>>().inner().clone();
    blocking(move || state.run_action(&runner, "test", &run_id, &request)).await
}

#[tauri::command]
fn agent_cancel(app: AppHandle, run_id: String) -> bool {
    app.state::<Arc<agent::AgentState>>().cancel(&run_id)
}

#[tauri::command]
async fn watch_start(app: AppHandle) -> Result<WatchStatus> {
    let state = app.state::<Arc<CollectorState>>().inner().clone();
    let runner = runner(&app)?;
    blocking(move || state.start_watch(&app, &runner)).await
}

#[tauri::command]
async fn watch_stop(app: AppHandle) -> Result<WatchStatus> {
    let state = app.state::<Arc<CollectorState>>().inner().clone();
    blocking(move || state.stop_watch(&app)).await
}

#[tauri::command]
fn watch_status(app: AppHandle) -> WatchStatus {
    app.state::<Arc<CollectorState>>().status()
}

#[tauri::command]
async fn python_detect() -> Result<Vec<PythonCandidate>> {
    blocking(|| Ok(collector::detect_python())).await
}

#[tauri::command]
async fn python_check(path: String) -> Result<PythonCandidate> {
    blocking(move || Ok(collector::check_python(&path))).await
}

/// The interpreter the collector would use now, checked (Settings shows it).
#[tauri::command]
async fn python_status(app: AppHandle) -> Result<Value> {
    let choice = python_choice(&app)?;
    blocking(move || {
        let checked = collector::check_python(&choice.path);
        Ok(serde_json::json!({"source": choice.source, "path": choice.path, "version": checked.version, "ok": checked.ok, "error": checked.error}))
    }).await
}

// ----------------------------------------------------------------------------- opening files

fn openable(path: &Path) -> Result<()> {
    if !path.is_absolute() || !path.exists() {
        return Err(format!("This path is unavailable on this computer: {}", path.display()));
    }
    if path.is_file() {
        let extension = path.extension().map(|e| e.to_string_lossy().to_lowercase()).unwrap_or_default();
        if BLOCKED_EXTENSIONS.contains(&extension.as_str()) {
            return Err("Programs and scripts are not opened from the dashboard; their folder is shown instead.".into());
        }
    }
    Ok(())
}

#[tauri::command]
fn open_path(app: AppHandle, path: String) -> Result<()> {
    let target = PathBuf::from(&path);
    if let Err(error) = openable(&target) {
        if target.is_file() {
            return app.opener().reveal_item_in_dir(&target).map_err(|e| e.to_string());
        }
        return Err(error);
    }
    app.opener().open_path(path, None::<&str>).map_err(|e| e.to_string())
}

#[tauri::command]
fn reveal_path(app: AppHandle, path: String) -> Result<()> {
    let target = PathBuf::from(&path);
    if !target.is_absolute() || !target.exists() {
        return Err(format!("This path is unavailable on this computer: {path}"));
    }
    app.opener().reveal_item_in_dir(target).map_err(|e| e.to_string())
}

/// "dir", "file" or "missing" for an absolute path (a folder dropped on the window is checked before connecting).
#[tauri::command]
fn path_kind(path: String) -> &'static str {
    let target = Path::new(&path);
    if !target.is_absolute() {
        return "missing";
    }
    match std::fs::metadata(target) {
        Ok(meta) if meta.is_dir() => "dir",
        Ok(_) => "file",
        Err(_) => "missing",
    }
}

#[tauri::command]
fn open_url(app: AppHandle, url: String) -> Result<()> {
    let lower = url.to_lowercase();
    if !["https://", "http://", "vscode://", "mailto:"].iter().any(|scheme| lower.starts_with(scheme)) {
        return Err("Only web, mail and VS Code links can be opened".into());
    }
    app.opener().open_url(url, None::<&str>).map_err(|e| e.to_string())
}

/// Resolve a source's folder (or a file inside it) through this computer's mapping and open it.
#[tauri::command]
async fn open_source(app: AppHandle, source_id: String, relative: Option<String>) -> Result<()> {
    let root = workspace_root(&app)?;
    let target = blocking(move || resolve_source(&root, &source_id, relative.as_deref())).await?;
    open_path(app, target.display().to_string())
}

fn resolve_source(root: &Path, source_id: &str, relative: Option<&str>) -> Result<PathBuf> {
    fsutil::require_id(source_id, "Source ID")?;
    let config: Value = serde_json::from_str(&fsutil::read_bounded(&root.join(format!("sources/{source_id}.json")), 512 * 1024)?)
        .map_err(|e| e.to_string())?;
    let host = config.pointer("/collector/hostId").and_then(Value::as_str).ok_or("The source has no collector host")?;
    fsutil::require_id(host, "Host ID")?;
    let alias = config.pointer("/source/rootAlias").and_then(Value::as_str).ok_or("The source has no folder mapping")?;
    let sub = config.pointer("/source/path").and_then(Value::as_str).unwrap_or("");
    let local: Value = serde_json::from_str(&fsutil::read_bounded(&root.join(format!("local/{host}.json")), 512 * 1024)?)
        .map_err(|_| "Open this source on its collection computer.".to_string())?;
    let base = local.pointer(&format!("/aliases/{}", alias.replace('~', "~0").replace('/', "~1"))).and_then(Value::as_str)
        .ok_or("The source folder is not mapped on this computer")?;
    let mut folder = PathBuf::from(base);
    if !sub.is_empty() {
        folder = fsutil::contained(&folder, sub)?;
    }
    let target = match relative.filter(|r| !r.is_empty()) {
        Some(rel) => fsutil::contained(&folder, &rel.replace('\\', "/"))?,
        None => folder,
    };
    if !target.exists() {
        return Err("The source file is unavailable on this computer".into());
    }
    Ok(target)
}

// ----------------------------------------------------------------------------- windows

/// Async on purpose: building a window from a synchronous command (main thread) deadlocks on Windows.
#[tauri::command]
async fn widget_set_visible(app: AppHandle, visible: bool) -> Result<bool> {
    windows::set_widget_visible(&app, visible)
}

#[tauri::command]
fn widget_pointer(app: AppHandle) -> Option<bool> {
    windows::widget_pointer_inside(&app)
}

/// The widget's Connect button or a folder dropped on it: open the plan card in the main window.
#[tauri::command]
fn connect_in_main(app: AppHandle, source: Option<String>) {
    windows::show_main(&app);
    let _ = app.emit_to(windows::MAIN, "rd://connect", serde_json::json!({ "source": source }));
}

#[tauri::command]
fn navigate_main(app: AppHandle, target: NavigateTarget) {
    windows::navigate(&app, target);
}

#[tauri::command]
fn quit_app(app: AppHandle) {
    quit(&app);
}

pub(crate) fn quit(app: &AppHandle) {
    app.state::<Arc<CollectorState>>().kill_now();
    app.state::<Arc<agent::AgentState>>().cancel_all();
    app.exit(0);
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, args, _cwd| match request_from_args(&args) {
            Some(request) => deliver_request(app, request),
            None => windows::show_main(app),
        }))
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_autostart::init(tauri_plugin_autostart::MacosLauncher::LaunchAgent, Some(vec![AUTOSTART_FLAG])))
        .manage(Arc::new(CollectorState::default()))
        .manage(Arc::new(agent::AgentState::default()))
        .manage(fswatch::WatchState::default())
        .manage(tray::TrayHolder(Mutex::new(None)))
        .manage(PendingRequests::default())
        .setup(|app| {
            let handle = app.handle().clone();
            app.manage(SettingsState::load(&handle));
            let settings = settings_state(&handle).get();
            if let Err(error) = tray::create(&handle) {
                eprintln!("Tray icon unavailable: {error}");
            }
            if let Some(path) = settings.workspace.as_deref() {
                if Path::new(path).join(workspace::MARKER).is_file() {
                    start_workspace_watch(&handle, Path::new(path));
                }
            }
            write_cli_locator(&handle);
            skill::refresh_installed(&handle);
            let startup: Vec<String> = std::env::args().collect();
            if let Some(request) = request_from_args(&startup) {
                if let Ok(mut pending) = handle.state::<PendingRequests>().0.lock() {
                    pending.push(request);
                }
            }
            let autostarted = std::env::args().any(|a| a == AUTOSTART_FLAG);
            if !autostarted {
                windows::show_main(&handle);
            }
            if settings.widget.visible || (autostarted && !settings.close_to_tray) {
                let _ = windows::set_widget_visible(&handle, true);
            }
            if settings.start_live_on_launch {
                let app = handle.clone();
                tauri::async_runtime::spawn_blocking(move || {
                    let state = app.state::<Arc<CollectorState>>().inner().clone();
                    if let Err(error) = runner(&app).and_then(|r| state.start_watch(&app, &r)) {
                        let _ = app.emit("app://error", error);
                    }
                });
            }
            Ok(())
        })
        .on_window_event(|window, event| {
            if window.label() == windows::MAIN {
                if let WindowEvent::CloseRequested { api, .. } = event {
                    let app = window.app_handle();
                    if settings_state(app).get().close_to_tray {
                        api.prevent_close();
                        let _ = window.hide();
                    } else {
                        quit(app);
                    }
                }
            } else if window.label() == windows::WIDGET {
                match event {
                    WindowEvent::CloseRequested { .. } | WindowEvent::Destroyed => tray::sync(window.app_handle()),
                    WindowEvent::Moved(_) | WindowEvent::Resized(_) => windows::remember_widget_bounds(window),
                    _ => {}
                }
            }
        })
        .invoke_handler(tauri::generate_handler![
            get_settings,
            update_settings,
            app_info,
            workspace_current,
            workspace_create,
            workspace_open,
            workspace_snapshot,
            project_update,
            milestones_write,
            project_remove,
            source_remap,
            collector_run,
            agent_key_status,
            agent_key_set,
            agent_propose,
            agent_test,
            agent_cancel,
            watch_start,
            watch_stop,
            watch_status,
            python_detect,
            python_check,
            python_status,
            open_path,
            reveal_path,
            path_kind,
            open_url,
            open_source,
            widget_set_visible,
            widget_pointer,
            connect_in_main,
            take_requests,
            skill_status,
            skill_install,
            skill_uninstall,
            navigate_main,
            quit_app,
        ])
        .build(tauri::generate_context!())
        .expect("error while building the Monitoring Anything application");
    app.run(|handle, event| {
        if let RunEvent::Exit = event {
            handle.state::<Arc<CollectorState>>().kill_now();
            handle.state::<Arc<agent::AgentState>>().cancel_all();
            handle.state::<fswatch::WatchState>().stop();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rd_requests_are_read_once_and_must_be_connect() {
        let dir = tempfile::tempdir().unwrap();
        let file = dir.path().join("req.json");
        std::fs::write(&file, r#"{"action": "connect", "source": "C:/p"}"#).unwrap();
        let args = vec!["app".to_string(), REQUEST_FLAG.to_string(), file.display().to_string()];
        assert_eq!(request_from_args(&args).unwrap()["source"], "C:/p");
        assert!(!file.exists());
        std::fs::write(&file, r#"{"action": "delete-everything"}"#).unwrap();
        assert!(request_from_args(&args).is_none());
        assert!(request_from_args(&["app".to_string()]).is_none());
    }

    #[test]
    fn programs_are_never_opened() {
        let dir = tempfile::tempdir().unwrap();
        let script = dir.path().join("run.bat");
        std::fs::write(&script, "echo hi").unwrap();
        let doc = dir.path().join("paper.pdf");
        std::fs::write(&doc, "%PDF").unwrap();
        assert!(openable(&script).is_err());
        assert!(openable(&doc).is_ok());
        assert!(openable(dir.path()).is_ok());
        assert!(openable(Path::new("relative.pdf")).is_err());
    }

    #[test]
    fn sources_resolve_through_the_local_mapping_without_escaping() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("ws");
        let paper = dir.path().join("paper");
        std::fs::create_dir_all(root.join("sources")).unwrap();
        std::fs::create_dir_all(root.join("local")).unwrap();
        std::fs::create_dir_all(paper.join("Tex")).unwrap();
        std::fs::write(paper.join("Tex/intro.tex"), "x").unwrap();
        std::fs::write(root.join("sources/paper.json"), r#"{"source": {"rootAlias": "paper", "path": ""}, "collector": {"hostId": "host"}}"#).unwrap();
        let mapping = serde_json::json!({"schemaVersion": "1.0", "aliases": {"paper": paper.display().to_string()}});
        std::fs::write(root.join("local/host.json"), mapping.to_string()).unwrap();
        assert_eq!(resolve_source(&root, "paper", Some("Tex/intro.tex")).unwrap(), paper.join("Tex/intro.tex"));
        assert_eq!(resolve_source(&root, "paper", None).unwrap(), paper);
        assert!(resolve_source(&root, "paper", Some("../ws/sources/paper.json")).is_err());
        assert!(resolve_source(&root, "paper", Some("missing.tex")).is_err());
        assert!(resolve_source(&root, "../x", None).is_err());
    }
}
