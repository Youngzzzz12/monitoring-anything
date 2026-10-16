//! Per-user application settings, stored as JSON in the platform config folder
//! (never inside the workspace, so a synced workspace stays machine-neutral).

use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::PathBuf;
use std::sync::Mutex;
use tauri::{AppHandle, Manager};

use crate::fsutil::{atomic_write, read_bounded};

/// 2: `pythonPath` became a user override (version 1 saved the auto-detected interpreter there).
/// 3: live updates start at launch by default (an edited section otherwise waits for a manual start).
const SCHEMA_VERSION: u32 = 3;

/// The widget's last position and size (logical pixels), restored when it opens again.
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct WidgetBounds {
    pub x: f64,
    pub y: f64,
    pub width: f64,
    pub height: f64,
}

pub const WIDGET_SIZE: (f64, f64) = (320.0, 540.0);
pub const WIDGET_MIN: (f64, f64) = (260.0, 320.0);

impl WidgetBounds {
    /// Finite, at least the minimum size and not absurdly large; None otherwise.
    pub fn sane(self) -> Option<Self> {
        let finite = [self.x, self.y, self.width, self.height].iter().all(|v| v.is_finite());
        (finite && self.width >= WIDGET_MIN.0 && self.height >= WIDGET_MIN.1 && self.width <= 4000.0 && self.height <= 4000.0
            && self.x.abs() < 100_000.0 && self.y.abs() < 100_000.0).then_some(self)
    }
}

/// Where the circle widget sits on screen (its top-left corner, logical pixels).
#[derive(Clone, Copy, Debug, Serialize, Deserialize, PartialEq)]
pub struct OrbPoint {
    pub x: f64,
    pub y: f64,
}

impl OrbPoint {
    pub fn sane(self) -> Option<Self> {
        (self.x.is_finite() && self.y.is_finite() && self.x.abs() < 100_000.0 && self.y.abs() < 100_000.0).then_some(self)
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct WidgetSettings {
    pub visible: bool,
    pub keep_on_top: bool,
    pub fade_when_idle: bool,
    /// Opacity while idle, 0.3-0.9.
    pub idle_opacity: f64,
    /// The project the widget focuses on (with its sub-projects); None shows all projects.
    pub focus_project_id: Option<String>,
    /// Dock-like growth of the section under the pointer, 0 (off) to 0.08.
    pub magnification: f64,
    /// Where the widget was last placed; None opens it at the default size.
    pub bounds: Option<WidgetBounds>,
    /// Shrunk to a circle showing only the completion ring (hover shows the breakdown, click expands).
    pub collapsed: bool,
    /// Where the circle was last placed.
    pub orb: Option<OrbPoint>,
}

impl Default for WidgetSettings {
    fn default() -> Self {
        Self { visible: false, keep_on_top: true, fade_when_idle: true, idle_opacity: 0.55, focus_project_id: None, magnification: 0.05, bounds: None, collapsed: false, orb: None }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "camelCase", default)]
pub struct AppSettings {
    pub schema_version: u32,
    /// Absolute path of the open workspace (folder containing workspace.json).
    pub workspace: Option<String>,
    /// Recently opened workspaces, newest first.
    pub recent_workspaces: Vec<String>,
    /// A Python 3.10+ executable chosen by the user; empty means the bundled runtime (or a detected system Python).
    pub python_path: Option<String>,
    /// "full" | "subtle" | "off" (the OS reduce-motion setting always wins).
    pub motion: String,
    /// "system" | "light" | "dark".
    pub theme: String,
    pub plain_backdrop: bool,
    pub close_to_tray: bool,
    pub start_live_on_launch: bool,
    pub widget: WidgetSettings,
    /// Agent provider choice, per-provider overrides, never-send patterns and time limit (shape owned by the UI;
    /// keys are not here but in the OS credential store).
    pub agent: Value,
    /// Scan discovery folders for new sub-project folders in the background.
    pub discovery_scan: bool,
}

impl Default for AppSettings {
    fn default() -> Self {
        Self {
            schema_version: SCHEMA_VERSION,
            workspace: None,
            recent_workspaces: Vec::new(),
            python_path: None,
            motion: "full".into(),
            theme: "system".into(),
            plain_backdrop: false,
            close_to_tray: true,
            start_live_on_launch: true,
            widget: WidgetSettings::default(),
            agent: Value::Object(Default::default()),
            discovery_scan: true,
        }
    }
}

impl AppSettings {
    /// Clamp values a hand-edited or older file may carry.
    pub fn normalized(mut self) -> Self {
        if self.schema_version < 2 {
            self.python_path = None;
        }
        if self.schema_version < 3 {
            self.start_live_on_launch = true;
        }
        self.schema_version = SCHEMA_VERSION;
        if !["full", "subtle", "off"].contains(&self.motion.as_str()) {
            self.motion = "full".into();
        }
        if !["system", "light", "dark"].contains(&self.theme.as_str()) {
            self.theme = "system".into();
        }
        if !self.widget.idle_opacity.is_finite() {
            self.widget.idle_opacity = 0.55;
        }
        self.widget.idle_opacity = self.widget.idle_opacity.clamp(0.3, 0.9);
        if !self.widget.magnification.is_finite() {
            self.widget.magnification = 0.05;
        }
        self.widget.magnification = self.widget.magnification.clamp(0.0, 0.08);
        self.widget.bounds = self.widget.bounds.and_then(WidgetBounds::sane);
        self.widget.orb = self.widget.orb.and_then(OrbPoint::sane);
        self.widget.focus_project_id = self.widget.focus_project_id.take().filter(|id| crate::fsutil::require_id(id, "Project ID").is_ok());
        self.recent_workspaces.truncate(8);
        self.python_path = self.python_path.filter(|p| !p.trim().is_empty());
        if !self.agent.is_object() {
            self.agent = Value::Object(Default::default());
        }
        if let Some(agent) = self.agent.as_object_mut() {
            for secret in ["apiKey", "apiKeys", "agentKeys"] {
                agent.remove(secret);
            }
        }
        self
    }

    /// Merge a JSON patch of known keys (unknown keys are rejected).
    pub fn merged(&self, patch: &Value) -> Result<Self, String> {
        let patch = patch.as_object().ok_or("Settings patch must be an object")?;
        let mut current = serde_json::to_value(self).map_err(|e| e.to_string())?;
        let target = current.as_object_mut().ok_or("Settings are not an object")?;
        for (key, value) in patch {
            if key == "schemaVersion" || !target.contains_key(key) {
                return Err(format!("Unknown setting: {key}"));
            }
            if key == "widget" {
                let widget = target.get_mut("widget").and_then(Value::as_object_mut).ok_or("Invalid widget settings")?;
                for (inner, inner_value) in value.as_object().ok_or("widget must be an object")? {
                    if !widget.contains_key(inner) {
                        return Err(format!("Unknown widget setting: {inner}"));
                    }
                    widget.insert(inner.clone(), inner_value.clone());
                }
            } else {
                target.insert(key.clone(), value.clone());
            }
        }
        let next: AppSettings = serde_json::from_value(current).map_err(|e| format!("Invalid setting value: {e}"))?;
        Ok(next.normalized())
    }

    pub fn remember_workspace(&mut self, path: &str) {
        self.workspace = Some(path.to_owned());
        self.recent_workspaces.retain(|item| item != path);
        self.recent_workspaces.insert(0, path.to_owned());
        self.recent_workspaces.truncate(8);
    }
}

/// Settings written before the rename (config folder `io.github.research-dashboard`) are copied once.
fn migrate_legacy(dir: &std::path::Path, path: &std::path::Path) {
    let legacy = dir.with_file_name("io.github.research-dashboard").join("settings.json");
    if !path.exists() && legacy.is_file() && std::fs::create_dir_all(dir).is_ok() {
        let _ = std::fs::copy(&legacy, path);
    }
}

pub struct SettingsState {
    pub path: PathBuf,
    pub value: Mutex<AppSettings>,
}

impl SettingsState {
    pub fn load(app: &AppHandle) -> Self {
        let dir = app.path().app_config_dir().unwrap_or_else(|_| std::env::temp_dir().join("monitoring-anything"));
        let path = dir.join("settings.json");
        migrate_legacy(&dir, &path);
        let value = read_bounded(&path, 256 * 1024)
            .ok()
            .and_then(|text| serde_json::from_str::<AppSettings>(&text).ok())
            .unwrap_or_default()
            .normalized();
        Self { path, value: Mutex::new(value) }
    }

    pub fn get(&self) -> AppSettings {
        self.value.lock().map(|v| v.clone()).unwrap_or_default()
    }

    pub fn save(&self, next: AppSettings) -> Result<AppSettings, String> {
        let text = serde_json::to_string_pretty(&next).map_err(|e| e.to_string())?;
        atomic_write(&self.path, format!("{text}\n").as_bytes())?;
        if let Ok(mut guard) = self.value.lock() {
            *guard = next.clone();
        }
        Ok(next)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn patch_merges_known_keys_and_clamps() {
        let base = AppSettings::default();
        let next = base.merged(&json!({"motion": "subtle", "widget": {"idleOpacity": 2.0}})).unwrap();
        assert_eq!(next.motion, "subtle");
        assert_eq!(next.widget.idle_opacity, 0.9);
        assert_eq!(base.merged(&json!({"widget": {"magnification": 0.5}})).unwrap().widget.magnification, 0.08);
        assert!(next.widget.keep_on_top);
        assert!(base.merged(&json!({"nope": 1})).is_err());
        assert!(base.merged(&json!({"widget": {"nope": 1}})).is_err());
        assert!(base.merged(&json!({"motion": 3})).is_err());
        assert_eq!(base.merged(&json!({"theme": "neon"})).unwrap().theme, "system");
        let agent = base.merged(&json!({"agent": {"providerId": "codex", "apiKey": "leak"}})).unwrap().agent;
        assert_eq!(agent["providerId"], "codex");
        assert!(agent.get("apiKey").is_none());
        assert!(base.merged(&json!({"agent": 3})).unwrap().agent.is_object());
        assert_eq!(base.merged(&json!({"widget": {"focusProjectId": "phd_thesis"}})).unwrap().widget.focus_project_id.as_deref(), Some("phd_thesis"));
        assert_eq!(base.merged(&json!({"widget": {"focusProjectId": "../x"}})).unwrap().widget.focus_project_id, None);
    }

    #[test]
    fn version_one_files_drop_the_detected_python() {
        let parsed: AppSettings = serde_json::from_str("{\"schemaVersion\": 1, \"pythonPath\": \"C:/py/python.exe\"}").unwrap();
        let normalized = parsed.normalized();
        assert_eq!(normalized.python_path, None);
        assert_eq!(normalized.schema_version, SCHEMA_VERSION);
        assert!(normalized.start_live_on_launch);
        let kept = AppSettings { python_path: Some("C:/py/python.exe".into()), ..AppSettings::default() }.normalized();
        assert_eq!(kept.python_path.as_deref(), Some("C:/py/python.exe"));
    }

    #[test]
    fn widget_bounds_are_kept_only_when_sane() {
        let ok = WidgetBounds { x: 100.0, y: 80.0, width: 300.0, height: 500.0 };
        assert_eq!(ok.sane(), Some(ok));
        assert_eq!(WidgetBounds { width: 100.0, ..ok }.sane(), None);
        assert_eq!(WidgetBounds { x: f64::NAN, ..ok }.sane(), None);
        let mut settings = AppSettings::default();
        settings.widget.bounds = Some(WidgetBounds { height: 9000.0, ..ok });
        assert_eq!(settings.normalized().widget.bounds, None);
    }

    #[test]
    fn old_files_fill_defaults() {
        let parsed: AppSettings = serde_json::from_str("{\"workspace\": \"C:/w\"}").unwrap();
        assert_eq!(parsed.workspace.as_deref(), Some("C:/w"));
        assert_eq!(parsed.motion, "full");
        assert!(parsed.close_to_tray);
    }

    #[test]
    fn recent_workspaces_are_unique_and_newest_first() {
        let mut settings = AppSettings::default();
        settings.remember_workspace("a");
        settings.remember_workspace("b");
        settings.remember_workspace("a");
        assert_eq!(settings.recent_workspaces, vec!["a", "b"]);
    }
}
