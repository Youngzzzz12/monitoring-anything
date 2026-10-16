//! The app-owned workspace folder: creation, a read-only snapshot for the UI,
//! guarded writes to project records and milestone lists, and recoverable removal.
//!
//! Layout (see schemas/ and collector/tools/layout.py):
//! workspace.json, projects/<id>/project.json, projects/<id>/milestones.md,
//! sources/<id>.json, local/<host>.json, data/<id>.json, profiles/user/.

use serde::Serialize;
use serde_json::{json, Map, Value};
use std::fs;
use std::path::{Path, PathBuf};

use crate::fsutil::{atomic_write, contained, read_bounded, require_id, utc_iso, utc_stamp, valid_id};

pub const MARKER: &str = "workspace.json";
const DIRS: [&str; 6] = ["projects", "sources", "local", "data/history", "profiles/user", "profiles/milestones/user"];
const MAX_RECORD: u64 = 1024 * 1024;
const MAX_MILESTONES: u64 = 2 * 1024 * 1024;
const MAX_CONFIG: u64 = 512 * 1024;
const MAX_STATE: u64 = 16 * 1024 * 1024;
const MAX_ENTRIES: usize = 2000;
const README: &str = include_str!("../../schemas/workspace-README.md");

#[derive(Serialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
pub struct WorkspaceInfo {
    pub root: String,
    pub name: String,
    pub created: bool,
    pub meta: Value,
}

#[derive(Serialize, Debug, Default)]
#[serde(rename_all = "camelCase")]
pub struct ProjectEntry {
    pub id: String,
    pub record: Option<Value>,
    pub record_error: Option<String>,
    pub milestones: Option<String>,
    pub milestones_error: Option<String>,
}

#[derive(Serialize, Debug, Default)]
#[serde(rename_all = "camelCase")]
pub struct JsonEntry {
    pub id: String,
    pub value: Option<Value>,
    pub error: Option<String>,
}

#[derive(Serialize, Debug, Default)]
#[serde(rename_all = "camelCase")]
pub struct ProfileTitle {
    pub id: String,
    pub title: String,
    pub origin: String,
}

#[derive(Serialize, Debug, Default)]
#[serde(rename_all = "camelCase")]
pub struct Snapshot {
    pub root: String,
    pub name: String,
    pub projects: Vec<ProjectEntry>,
    pub sources: Vec<JsonEntry>,
    pub states: Vec<JsonEntry>,
    pub local: Vec<JsonEntry>,
    pub profiles: Vec<ProfileTitle>,
    pub errors: Vec<String>,
    pub loaded_at: String,
}

/// The marker's `kind`: the current name first; workspaces created under the earlier name stay valid.
const WORKSPACE_KINDS: [&str; 2] = ["monitoring-anything-workspace", "research-dashboard-workspace"];

fn read_marker(root: &Path) -> Result<Value, String> {
    let text = read_bounded(&root.join(MARKER), 64 * 1024)?;
    let value: Value = serde_json::from_str(&text).map_err(|e| format!("workspace.json is not valid JSON: {e}"))?;
    if !value.get("kind").and_then(Value::as_str).is_some_and(|kind| WORKSPACE_KINDS.contains(&kind)) {
        return Err("This folder's workspace.json does not belong to Monitoring Anything".into());
    }
    Ok(value)
}

fn info(root: &Path, meta: Value, created: bool) -> WorkspaceInfo {
    let name = meta
        .get("name")
        .and_then(Value::as_str)
        .map(str::to_owned)
        .unwrap_or_else(|| root.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default());
    WorkspaceInfo { root: root.display().to_string(), name, created, meta }
}

/// Open an existing workspace (a folder with a Monitoring Anything workspace.json).
pub fn open(path: &str) -> Result<WorkspaceInfo, String> {
    let root = PathBuf::from(path);
    if !root.is_absolute() {
        return Err("Choose an absolute workspace folder".into());
    }
    let root = root.canonicalize().map_err(|_| "The workspace folder does not exist".to_string())?;
    let meta = read_marker(&root)?;
    for folder in DIRS {
        let _ = fs::create_dir_all(root.join(folder));
    }
    Ok(info(&clean(root), meta, false))
}

/// Create a workspace in an empty folder (or reopen one that already is a workspace).
pub fn create(path: &str, name: Option<String>) -> Result<WorkspaceInfo, String> {
    let root = PathBuf::from(path);
    if !root.is_absolute() {
        return Err("Choose an absolute workspace folder".into());
    }
    if root.join(MARKER).is_file() {
        return open(path);
    }
    if root.exists() {
        if !root.is_dir() {
            return Err("The workspace path is a file".into());
        }
        let occupied = fs::read_dir(&root)
            .map_err(|e| e.to_string())?
            .filter_map(Result::ok)
            .any(|entry| !entry.file_name().to_string_lossy().starts_with('.'));
        if occupied {
            return Err("Choose an empty folder for a new workspace, or open an existing workspace".into());
        }
    }
    fs::create_dir_all(&root).map_err(|e| format!("Could not create the workspace folder: {e}"))?;
    let root = clean(root.canonicalize().map_err(|e| e.to_string())?);
    for folder in DIRS {
        fs::create_dir_all(root.join(folder)).map_err(|e| e.to_string())?;
    }
    let display = name
        .map(|n| n.trim().chars().take(200).collect::<String>())
        .filter(|n| !n.is_empty())
        .unwrap_or_else(|| root.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| "Research".into()));
    let mut meta = Map::new();
    meta.insert("schemaVersion".into(), Value::from("1.0"));
    meta.insert("kind".into(), Value::from(WORKSPACE_KINDS[0]));
    meta.insert("name".into(), Value::from(display));
    meta.insert("layout".into(), Value::from("app"));
    meta.insert("createdAt".into(), Value::from(utc_iso()));
    let meta = Value::Object(meta);
    atomic_write(&root.join("README.md"), README.as_bytes())?;
    atomic_write(&root.join(MARKER), format!("{}\n", serde_json::to_string_pretty(&meta).unwrap()).as_bytes())?;
    Ok(info(&root, meta, true))
}

/// Drop the `\\?\` prefix Windows canonicalization adds, so paths stay readable and Python-friendly.
fn clean(path: PathBuf) -> PathBuf {
    let text = path.display().to_string();
    match text.strip_prefix(r"\\?\") {
        Some(rest) if !rest.starts_with("UNC\\") => PathBuf::from(rest),
        _ => path,
    }
}

fn read_json(path: &Path, max: u64) -> Result<Value, String> {
    let text = read_bounded(path, max)?;
    let value: Value = serde_json::from_str(&text).map_err(|e| format!("{}: invalid JSON ({e})", path.display()))?;
    if !value.is_object() {
        return Err(format!("{}: expected a JSON object", path.display()));
    }
    Ok(value)
}

fn json_dir(root: &Path, relative: &str, max: u64, errors: &mut Vec<String>) -> Vec<JsonEntry> {
    let mut entries = Vec::new();
    let Ok(listing) = fs::read_dir(root.join(relative)) else { return entries };
    let mut paths: Vec<PathBuf> = listing
        .filter_map(Result::ok)
        .map(|entry| entry.path())
        .filter(|path| path.is_file() && path.extension().map(|e| e == "json").unwrap_or(false))
        .collect();
    paths.sort();
    if paths.len() > MAX_ENTRIES {
        errors.push(format!("{relative}: only the first {MAX_ENTRIES} files were read"));
        paths.truncate(MAX_ENTRIES);
    }
    for path in paths {
        let id = path.file_stem().map(|s| s.to_string_lossy().into_owned()).unwrap_or_default();
        if id.starts_with('.') {
            continue;
        }
        match read_json(&path, max) {
            Ok(value) => entries.push(JsonEntry { id, value: Some(value), error: None }),
            Err(error) => entries.push(JsonEntry { id, value: None, error: Some(error) }),
        }
    }
    entries
}

fn profile_titles(dir: &Path, origin: &str, out: &mut Vec<ProfileTitle>) {
    let Ok(listing) = fs::read_dir(dir) else { return };
    for entry in listing.filter_map(Result::ok).take(MAX_ENTRIES) {
        let path = entry.path();
        if path.extension().map(|e| e != "json").unwrap_or(true) {
            continue;
        }
        if let Ok(value) = read_json(&path, 256 * 1024) {
            let id = value.get("id").and_then(Value::as_str).unwrap_or_default();
            let title = value.get("title").and_then(Value::as_str).unwrap_or_default().trim();
            if valid_id(id) && !title.is_empty() {
                out.push(ProfileTitle { id: id.to_owned(), title: title.to_owned(), origin: origin.to_owned() });
            }
        }
    }
}

/// Everything the dashboard renders, read in one pass (bounded sizes, no writes).
pub fn snapshot(root: &Path, builtin_profiles: Option<&Path>) -> Result<Snapshot, String> {
    let meta = read_marker(root)?;
    let mut errors = Vec::new();
    let mut projects = Vec::new();
    if let Ok(listing) = fs::read_dir(root.join("projects")) {
        let mut folders: Vec<PathBuf> = listing.filter_map(Result::ok).map(|e| e.path()).filter(|p| p.is_dir()).collect();
        folders.sort();
        for folder in folders.into_iter().take(MAX_ENTRIES) {
            let id = folder.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default();
            if id.starts_with('.') {
                continue;
            }
            let record_path = folder.join("project.json");
            if !record_path.exists() {
                continue;
            }
            let mut entry = ProjectEntry { id: id.clone(), ..Default::default() };
            if !valid_id(&id) {
                entry.record_error = Some(format!("Invalid project folder name: {id}"));
            } else {
                match read_json(&record_path, MAX_RECORD) {
                    Ok(value) => entry.record = Some(value),
                    Err(error) => entry.record_error = Some(error),
                }
                let milestones = folder.join("milestones.md");
                if milestones.is_file() {
                    match read_bounded(&milestones, MAX_MILESTONES) {
                        Ok(text) => entry.milestones = Some(text),
                        Err(error) => entry.milestones_error = Some(error),
                    }
                }
            }
            projects.push(entry);
        }
    }
    let sources = json_dir(root, "sources", MAX_CONFIG, &mut errors);
    let states = json_dir(root, "data", MAX_STATE, &mut errors);
    let local = json_dir(root, "local", MAX_CONFIG, &mut errors);
    let mut profiles = Vec::new();
    if let Some(builtin) = builtin_profiles {
        profile_titles(builtin, "builtin", &mut profiles);
    }
    profile_titles(&root.join("profiles/user"), "user", &mut profiles);
    let workspace = info(root, meta, false);
    Ok(Snapshot {
        root: workspace.root,
        name: workspace.name,
        projects,
        sources,
        states,
        local,
        profiles,
        errors,
        loaded_at: utc_iso(),
    })
}

const RECORD_KEYS: [&str; 11] =
    ["title", "type", "parentId", "stage", "health", "nextStep", "due", "resources", "knowledge", "archived", "notes"];

fn check_patch_value(key: &str, value: &Value) -> Result<(), String> {
    let ok = match key {
        "title" => value.as_str().map(|s| !s.trim().is_empty() && s.chars().count() <= 200).unwrap_or(false),
        "type" | "stage" | "health" => value.as_str().map(|s| valid_id(s)).unwrap_or(false),
        "parentId" => value.is_null() || value.as_str().map(valid_id).unwrap_or(false),
        "nextStep" | "notes" => value.is_null() || value.as_str().map(|s| s.chars().count() <= 2000).unwrap_or(false),
        "due" => value.is_null() || value.as_str().map(is_iso_date).unwrap_or(false),
        "resources" | "knowledge" => value.as_array().map(|a| a.len() <= 200).unwrap_or(false),
        "archived" => value.is_boolean(),
        _ => false,
    };
    if ok {
        Ok(())
    } else {
        Err(format!("Invalid value for project field {key}"))
    }
}

fn is_iso_date(text: &str) -> bool {
    let b = text.as_bytes();
    b.len() == 10 && b[4] == b'-' && b[7] == b'-' && b.iter().enumerate().all(|(i, c)| i == 4 || i == 7 || c.is_ascii_digit())
}

/// Merge validated editable fields into projects/<id>/project.json (other keys, such as sourceIds, are kept).
pub fn update_record(root: &Path, id: &str, patch: &Value) -> Result<Value, String> {
    require_id(id, "Project ID")?;
    let patch = patch.as_object().ok_or("Project changes must be an object")?;
    for (key, value) in patch {
        if !RECORD_KEYS.contains(&key.as_str()) {
            return Err(format!("{key} cannot be edited here"));
        }
        check_patch_value(key, value)?;
    }
    if patch.get("parentId").and_then(Value::as_str) == Some(id) {
        return Err("A project cannot be its own parent".into());
    }
    let path = contained(root, &format!("projects/{id}/project.json"))?;
    let mut record = read_json(&path, MAX_RECORD).map_err(|_| "The project record is unavailable. Refresh and retry.".to_string())?;
    if record.get("projectId").and_then(Value::as_str) != Some(id) {
        return Err("The project record ID does not match its folder".into());
    }
    let object = record.as_object_mut().ok_or("The project record is not an object")?;
    for (key, value) in patch {
        object.insert(key.clone(), value.clone());
    }
    let text = serde_json::to_string_pretty(&record).map_err(|e| e.to_string())?;
    atomic_write(&path, format!("{text}\n").as_bytes())?;
    Ok(record)
}

/// Replace projects/<id>/milestones.md only if it still has the content the UI edited (optimistic concurrency).
pub fn write_milestones(root: &Path, id: &str, content: &str, expected: Option<&str>) -> Result<(), String> {
    require_id(id, "Project ID")?;
    if content.len() as u64 > MAX_MILESTONES {
        return Err("The milestone list is too large".into());
    }
    if !root.join(format!("projects/{id}/project.json")).is_file() {
        return Err("This project is no longer in the workspace".into());
    }
    let path = contained(root, &format!("projects/{id}/milestones.md"))?;
    let current = if path.exists() { Some(read_bounded(&path, MAX_MILESTONES)?) } else { None };
    let normalize = |text: &str| text.replace("\r\n", "\n");
    if current.as_deref().map(normalize) != expected.map(normalize) {
        return Err("The milestone list changed on disk. The dashboard has been refreshed; retry.".into());
    }
    atomic_write(&path, content.as_bytes())
}

/// Move projects/<id> into .trash/<stamp>-<id>/ (recoverable). Collector records are archived by `reconcile --apply`.
pub fn remove_project(root: &Path, id: &str) -> Result<String, String> {
    require_id(id, "Project ID")?;
    let folder = contained(root, &format!("projects/{id}"))?;
    if !folder.join("project.json").is_file() {
        return Err("The project folder is unavailable".into());
    }
    let relative = format!(".trash/{}-{id}/projects/{id}", utc_stamp());
    let destination = contained(root, &relative)?;
    fs::create_dir_all(destination.parent().unwrap()).map_err(|e| e.to_string())?;
    fs::rename(&folder, &destination).map_err(|e| format!("Could not move the project to the workspace trash: {e}"))?;
    Ok(relative)
}

/// Point a source at another folder on this computer (the paper moved, or another copy is the one being written).
/// The folder goes into this host's alias file; a source that addressed a subfolder of its alias is re-based on the
/// new folder. A LaTeX source's main file must exist there. The writing history measured the old folder, so it moves
/// to the workspace trash and tracking restarts. Returns the new folder, the history's trash path and warnings.
pub fn remap_source(root: &Path, source_id: &str, folder: &str) -> Result<Value, String> {
    require_id(source_id, "Source ID")?;
    let target = PathBuf::from(folder.trim().trim_matches('"'));
    if !target.is_absolute() || !target.is_dir() {
        return Err("Choose an existing folder on this computer.".into());
    }
    let target = clean(target);
    let config_path = contained(root, &format!("sources/{source_id}.json"))?;
    let mut config = read_json(&config_path, 512 * 1024)?;
    let host = config.pointer("/collector/hostId").and_then(Value::as_str).ok_or("The source has no collector host")?.to_string();
    require_id(&host, "Host ID")?;
    let alias = config.pointer("/source/rootAlias").and_then(Value::as_str).ok_or("The source has no folder mapping")?.to_string();
    if alias.is_empty() || alias.len() > 200 || alias.chars().any(|c| c == '/' || c == '\\' || c.is_control()) {
        return Err("The source's folder alias is invalid".into());
    }
    let mut warnings = Vec::new();
    if config.get("kind").and_then(Value::as_str) == Some("latex") {
        let main = config.pointer("/adapter/mainFile").and_then(Value::as_str).unwrap_or("main.tex");
        if !contained(&target, main).map(|path| path.is_file()).unwrap_or(false) {
            return Err(format!("{main} was not found in this folder. Choose the folder that contains it."));
        }
        let missing: Vec<String> = config.pointer("/adapter/chapters").and_then(Value::as_array).into_iter().flatten()
            .filter_map(Value::as_str).filter(|file| !contained(&target, file).map(|path| path.is_file()).unwrap_or(false))
            .map(str::to_owned).collect();
        if !missing.is_empty() {
            warnings.push(format!("{} listed file(s) are not in this folder: {}", missing.len(), missing.iter().take(4).cloned().collect::<Vec<_>>().join(", ")));
        }
    }
    let local_path = contained(root, &format!("local/{host}.json"))?;
    let mut local = if local_path.is_file() { read_json(&local_path, 512 * 1024)? } else { json!({"schemaVersion": "1.0", "aliases": {}}) };
    let aliases = local.as_object_mut().ok_or("The folder mapping file is not an object")?
        .entry("aliases").or_insert_with(|| json!({}));
    let aliases = aliases.as_object_mut().ok_or("The folder mapping file has no aliases object")?;
    let previous = aliases.get(&alias).and_then(Value::as_str).map(str::to_owned);
    aliases.insert(alias, Value::String(target.display().to_string()));
    fs::create_dir_all(local_path.parent().unwrap()).map_err(|e| e.to_string())?;
    atomic_write(&local_path, format!("{}\n", serde_json::to_string_pretty(&local).map_err(|e| e.to_string())?).as_bytes())?;
    if config.pointer("/source/path").and_then(Value::as_str).map(|p| !p.is_empty()).unwrap_or(false) {
        if let Some(source) = config.get_mut("source").and_then(Value::as_object_mut) {
            source.insert("path".into(), Value::String(String::new()));
        }
        atomic_write(&config_path, format!("{}\n", serde_json::to_string_pretty(&config).map_err(|e| e.to_string())?).as_bytes())?;
    }
    let history = contained(root, &format!("data/history/{source_id}.json"))?;
    let mut trashed = Value::Null;
    if history.is_file() {
        let relative = format!(".trash/{}-{source_id}/history.json", utc_stamp());
        let destination = contained(root, &relative)?;
        fs::create_dir_all(destination.parent().unwrap()).map_err(|e| e.to_string())?;
        fs::rename(&history, &destination).map_err(|e| format!("Could not reset the writing history: {e}"))?;
        trashed = Value::String(relative);
    }
    Ok(json!({"folder": target.display().to_string(), "previous": previous, "historyTrash": trashed, "warnings": warnings}))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sources_move_to_another_folder_and_restart_history() {
        let (dir, root) = workspace();
        let old = dir.path().join("old");
        let new = dir.path().join("new");
        for folder in [&old, &new] {
            fs::create_dir_all(folder.join("Tex")).unwrap();
            fs::write(folder.join("main.tex"), "x").unwrap();
        }
        fs::write(old.join("Tex/1.tex"), "x").unwrap();
        fs::create_dir_all(root.join("sources")).unwrap();
        fs::create_dir_all(root.join("local")).unwrap();
        fs::create_dir_all(root.join("data/history")).unwrap();
        let config = json!({"schemaVersion": "1.0", "projectId": "p", "kind": "latex", "source": {"rootAlias": "p", "path": ""},
                            "collector": {"hostId": "host"}, "adapter": {"mainFile": "main.tex", "chapters": ["main.tex", "Tex/1.tex"]}});
        fs::write(root.join("sources/p.json"), config.to_string()).unwrap();
        fs::write(root.join("local/host.json"), json!({"schemaVersion": "1.0", "aliases": {"p": old.display().to_string()}, "other": 1}).to_string()).unwrap();
        fs::write(root.join("data/history/p.json"), "{}").unwrap();
        let result = remap_source(&root, "p", new.to_str().unwrap()).unwrap();
        let local: Value = serde_json::from_str(&fs::read_to_string(root.join("local/host.json")).unwrap()).unwrap();
        assert_eq!(local["aliases"]["p"], new.display().to_string());
        assert_eq!(local["other"], 1);
        assert!(!root.join("data/history/p.json").exists());
        assert!(result["historyTrash"].as_str().unwrap().starts_with(".trash/"));
        assert_eq!(result["warnings"].as_array().unwrap().len(), 1);
        let empty = dir.path().join("empty");
        fs::create_dir_all(&empty).unwrap();
        assert!(remap_source(&root, "p", empty.to_str().unwrap()).unwrap_err().contains("main.tex"));
        assert!(remap_source(&root, "../p", new.to_str().unwrap()).is_err());
        assert!(remap_source(&root, "p", "relative").is_err());
    }

    fn workspace() -> (tempfile::TempDir, PathBuf) {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path().join("ws");
        let created = create(root.to_str().unwrap(), Some("Test".into())).unwrap();
        assert!(created.created);
        (dir, PathBuf::from(created.root))
    }

    fn add_project(root: &Path, id: &str) {
        let folder = root.join("projects").join(id);
        fs::create_dir_all(&folder).unwrap();
        let record = json!({"schemaVersion": "1.0", "projectId": id, "title": "T", "type": "paper", "parentId": null,
                            "stage": "planned", "health": "active", "nextStep": null, "due": null, "sourceIds": []});
        fs::write(folder.join("project.json"), serde_json::to_string_pretty(&record).unwrap()).unwrap();
        fs::write(folder.join("milestones.md"), "# Milestones\n\n- [ ] A #milestone\n").unwrap();
    }

    #[test]
    fn create_refuses_occupied_folders_and_reopens_workspaces() {
        let (dir, root) = workspace();
        assert!(root.join("projects").is_dir() && root.join("README.md").is_file());
        assert_eq!(open(root.to_str().unwrap()).unwrap().name, "Test");
        let occupied = dir.path().join("busy");
        fs::create_dir_all(&occupied).unwrap();
        fs::write(occupied.join("thesis.tex"), "x").unwrap();
        assert!(create(occupied.to_str().unwrap(), None).is_err());
        assert!(open(occupied.to_str().unwrap()).is_err());
        assert!(create("relative/path", None).is_err());
    }

    #[test]
    fn snapshot_reads_projects_sources_and_reports_errors() {
        let (_dir, root) = workspace();
        add_project(&root, "paper");
        fs::create_dir_all(root.join("projects/Bad Name")).unwrap();
        fs::write(root.join("projects/Bad Name/project.json"), "{}").unwrap();
        fs::write(root.join("sources/broken.json"), "{not json").unwrap();
        fs::write(root.join("data/paper.json"), "{\"schemaVersion\": \"1.0\"}").unwrap();
        let snap = snapshot(&root, None).unwrap();
        assert_eq!(snap.projects.len(), 2);
        let paper = snap.projects.iter().find(|p| p.id == "paper").unwrap();
        assert!(paper.record.is_some() && paper.milestones.as_deref().unwrap().contains("#milestone"));
        assert!(snap.projects.iter().any(|p| p.record_error.is_some()));
        assert!(snap.sources[0].error.is_some());
        assert_eq!(snap.states[0].id, "paper");
    }

    #[test]
    fn record_updates_are_validated_and_keep_other_keys() {
        let (_dir, root) = workspace();
        add_project(&root, "paper");
        let updated = update_record(&root, "paper", &json!({"health": "blocked", "due": "2026-12-01", "nextStep": "Write"})).unwrap();
        assert_eq!(updated["health"], "blocked");
        assert_eq!(updated["sourceIds"], json!([]));
        assert!(update_record(&root, "paper", &json!({"sourceIds": ["x"]})).is_err());
        assert!(update_record(&root, "paper", &json!({"due": "12/01/2026"})).is_err());
        assert!(update_record(&root, "paper", &json!({"parentId": "paper"})).is_err());
        assert!(update_record(&root, "../x", &json!({})).is_err());
        let text = fs::read_to_string(root.join("projects/paper/project.json")).unwrap();
        assert!(text.find("\"projectId\"").unwrap() < text.find("\"health\"").unwrap(), "key order is preserved");
    }

    #[test]
    fn milestone_writes_require_the_expected_content() {
        let (_dir, root) = workspace();
        add_project(&root, "paper");
        let before = fs::read_to_string(root.join("projects/paper/milestones.md")).unwrap();
        let after = before.replace("[ ]", "[x]");
        assert!(write_milestones(&root, "paper", &after, Some("stale")).is_err());
        write_milestones(&root, "paper", &after, Some(&before)).unwrap();
        assert!(fs::read_to_string(root.join("projects/paper/milestones.md")).unwrap().contains("[x]"));
    }

    #[test]
    fn removal_moves_the_folder_into_trash() {
        let (_dir, root) = workspace();
        add_project(&root, "paper");
        let moved = remove_project(&root, "paper").unwrap();
        assert!(!root.join("projects/paper").exists());
        assert!(root.join(&moved).join("project.json").is_file());
        assert!(remove_project(&root, "paper").is_err());
    }
}
