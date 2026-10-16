//! The `monitoring-anything` agent skill (skill/monitoring-anything/SKILL.md, bundled) for Claude Code and Codex.
//!
//! Installed only when the user asks (agent setup / Settings), into `~/.claude/skills/monitoring-anything` and
//! `~/.codex/skills/monitoring-anything` for the agents present on this computer. A marker file records that the app
//! installed it and what it wrote, so the app updates only its own unmodified copy (silently, on start) and never
//! touches a skill folder it did not create (for example one synced from a personal skill library).

use serde::Serialize;
use std::fs;
use std::path::{Path, PathBuf};
use tauri::{AppHandle, Manager};

use crate::fsutil::{atomic_write, read_bounded, utc_iso};

pub const NAME: &str = "monitoring-anything";
const MARKER: &str = ".installed-by-monitoring-anything.json";
const AGENTS: [(&str, &str, &str); 2] = [("claude", "Claude Code", ".claude"), ("codex", "Codex", ".codex")];

#[derive(Serialize, Clone, Debug, PartialEq)]
#[serde(rename_all = "camelCase")]
pub struct SkillTarget {
    pub agent: String,
    pub title: String,
    pub folder: String,
    /// The agent's home folder exists (it is installed or was used on this computer).
    pub agent_present: bool,
    /// "missing" | "installed" (current) | "outdated" (ours, older) | "modified" (ours, edited since) | "foreign"
    pub state: String,
}

/// FNV-1a: enough to tell whether a file changed (not a security check).
fn fingerprint(text: &str) -> String {
    let mut hash: u64 = 0xcbf29ce484222325;
    for byte in text.replace("\r\n", "\n").bytes() {
        hash ^= byte as u64;
        hash = hash.wrapping_mul(0x100000001b3);
    }
    format!("{hash:016x}")
}

fn home() -> Option<PathBuf> {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from)
}

/// The bundled SKILL.md (resources), or the source tree in development.
pub fn bundled(app: &AppHandle) -> Result<String, String> {
    let mut candidates = Vec::new();
    if let Ok(resources) = app.path().resource_dir() {
        candidates.push(resources.join("skill").join(NAME).join("SKILL.md"));
    }
    candidates.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("skill").join(NAME).join("SKILL.md"));
    candidates.iter().find(|path| path.is_file()).ok_or("The bundled skill was not found. Reinstall the app.".to_string())
        .and_then(|path| read_bounded(path, 256 * 1024))
}

/// One agent's state against the bundled text (pure, for tests).
pub fn target_state(folder: &Path, bundled: &str) -> String {
    let skill = folder.join("SKILL.md");
    if !folder.exists() {
        return "missing".into();
    }
    let marker: Option<serde_json::Value> = read_bounded(&folder.join(MARKER), 64 * 1024).ok().and_then(|t| serde_json::from_str(&t).ok());
    let current = read_bounded(&skill, 256 * 1024).unwrap_or_default();
    match marker.and_then(|m| m.get("fingerprint").and_then(|v| v.as_str()).map(str::to_owned)) {
        None => "foreign".into(),
        Some(written) if written != fingerprint(&current) => "modified".into(),
        Some(_) if fingerprint(&current) == fingerprint(bundled) => "installed".into(),
        Some(_) => "outdated".into(),
    }
}

pub fn status(app: &AppHandle) -> Result<Vec<SkillTarget>, String> {
    let text = bundled(app)?;
    let home = home().ok_or("The home folder is unknown")?;
    Ok(AGENTS.iter().map(|(agent, title, dot)| {
        let base = home.join(dot);
        let folder = base.join("skills").join(NAME);
        SkillTarget { agent: agent.to_string(), title: title.to_string(), folder: folder.display().to_string(),
                      agent_present: base.is_dir(), state: target_state(&folder, &text) }
    }).collect())
}

/// Write SKILL.md and the marker into one skill folder. Refuses a foreign or modified skill unless `force`.
pub fn install_into(folder: &Path, text: &str, version: &str, force: bool) -> Result<(), String> {
    let state = target_state(folder, text);
    if !force && (state == "foreign" || state == "modified") {
        return Err(format!("{} already has a monitoring-anything skill that the app did not install (or that was edited); not replacing it.", folder.display()));
    }
    fs::create_dir_all(folder).map_err(|e| format!("Could not create {}: {e}", folder.display()))?;
    atomic_write(&folder.join("SKILL.md"), text.as_bytes())?;
    let marker = serde_json::json!({"installedBy": "Monitoring Anything", "version": version, "fingerprint": fingerprint(text), "installedAt": utc_iso()});
    atomic_write(&folder.join(MARKER), format!("{marker}\n").as_bytes())
}

pub fn install(app: &AppHandle, agents: &[String], force: bool) -> Result<Vec<SkillTarget>, String> {
    let text = bundled(app)?;
    let version = app.package_info().version.to_string();
    let mut errors = Vec::new();
    for target in status(app)? {
        if agents.iter().any(|a| a == &target.agent) {
            if let Err(error) = install_into(Path::new(&target.folder), &text, &version, force) {
                errors.push(error);
            }
        }
    }
    if !errors.is_empty() {
        return Err(errors.join(" "));
    }
    status(app)
}

/// Remove the app's own copy (never a foreign skill).
pub fn uninstall(app: &AppHandle, agent: &str) -> Result<Vec<SkillTarget>, String> {
    let target = status(app)?.into_iter().find(|t| t.agent == agent).ok_or("Unknown agent")?;
    if !["installed", "outdated"].contains(&target.state.as_str()) {
        return Err("Only a skill the app installed (and that was not edited) is removed.".into());
    }
    fs::remove_dir_all(&target.folder).map_err(|e| e.to_string())?;
    status(app)
}

/// On start: bring the app's own, unedited copies up to date with this version.
pub fn refresh_installed(app: &AppHandle) {
    let Ok(targets) = status(app) else { return };
    let outdated: Vec<String> = targets.into_iter().filter(|t| t.state == "outdated").map(|t| t.agent).collect();
    if !outdated.is_empty() {
        let _ = install(app, &outdated, false);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn installs_updates_and_leaves_foreign_skills_alone() {
        let dir = tempfile::tempdir().unwrap();
        let folder = dir.path().join("skills").join(NAME);
        assert_eq!(target_state(&folder, "v1"), "missing");
        install_into(&folder, "v1", "0.1.0", false).unwrap();
        assert_eq!(target_state(&folder, "v1"), "installed");
        assert_eq!(target_state(&folder, "v2"), "outdated");
        install_into(&folder, "v2", "0.2.0", false).unwrap();
        assert_eq!(target_state(&folder, "v2"), "installed");
        fs::write(folder.join("SKILL.md"), "my own edits").unwrap();
        assert_eq!(target_state(&folder, "v2"), "modified");
        assert!(install_into(&folder, "v3", "0.3.0", false).is_err());
        let foreign = dir.path().join("other").join(NAME);
        fs::create_dir_all(&foreign).unwrap();
        fs::write(foreign.join("SKILL.md"), "synced from a library").unwrap();
        assert_eq!(target_state(&foreign, "v2"), "foreign");
        assert!(install_into(&foreign, "v2", "0.2.0", false).is_err());
        install_into(&foreign, "v2", "0.2.0", true).unwrap();
        assert_eq!(target_state(&foreign, "v2"), "installed");
        assert_eq!(fingerprint("a\r\nb"), fingerprint("a\nb"));
    }
}
