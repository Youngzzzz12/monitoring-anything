//! Watches the open workspace and tells every window when its files change
//! (edits from another window, the collector, a text editor or a sync client).

use notify::{RecommendedWatcher, RecursiveMode};
use notify_debouncer_mini::{new_debouncer, DebounceEventResult, Debouncer};
use std::path::{Path, PathBuf};
use std::sync::Mutex;
use std::time::Duration;
use tauri::{AppHandle, Emitter};

#[derive(Default)]
pub struct WatchState {
    debouncer: Mutex<Option<Debouncer<RecommendedWatcher>>>,
}

/// Paths that never affect what the dashboard shows.
pub fn relevant(root: &Path, path: &Path) -> Option<String> {
    let relative = path.strip_prefix(root).ok()?;
    let text = relative.to_string_lossy().replace('\\', "/");
    let name = relative.file_name()?.to_string_lossy();
    if text.is_empty()
        || text.starts_with(".trash")
        || text.starts_with("data/generated")
        || text.starts_with("data/agent-runs")
        || name.starts_with(".dashboard-")
        || name.ends_with(".tmp")
    {
        return None;
    }
    Some(text)
}

impl WatchState {
    pub fn watch(&self, app: &AppHandle, root: PathBuf) -> Result<(), String> {
        let emitter = app.clone();
        let base = root.clone();
        let mut debouncer = new_debouncer(Duration::from_millis(350), move |result: DebounceEventResult| {
            if let Ok(events) = result {
                let mut paths: Vec<String> = events.iter().filter_map(|event| relevant(&base, &event.path)).collect();
                paths.sort();
                paths.dedup();
                paths.truncate(100);
                if !paths.is_empty() {
                    let _ = emitter.emit("workspace://changed", paths);
                }
            }
        })
        .map_err(|e| format!("Workspace changes cannot be watched: {e}"))?;
        debouncer.watcher().watch(&root, RecursiveMode::Recursive).map_err(|e| format!("Workspace changes cannot be watched: {e}"))?;
        if let Ok(mut guard) = self.debouncer.lock() {
            *guard = Some(debouncer);
        }
        Ok(())
    }

    pub fn stop(&self) {
        if let Ok(mut guard) = self.debouncer.lock() {
            *guard = None;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn housekeeping_paths_are_ignored() {
        let root = Path::new("/w");
        assert_eq!(relevant(root, Path::new("/w/projects/a/project.json")).as_deref(), Some("projects/a/project.json"));
        assert_eq!(relevant(root, Path::new("/w/data/a.json")).as_deref(), Some("data/a.json"));
        assert!(relevant(root, Path::new("/w/.trash/x/project.json")).is_none());
        assert!(relevant(root, Path::new("/w/data/generated/a.md")).is_none());
        assert!(relevant(root, Path::new("/w/projects/a/.dashboard-1-2.tmp")).is_none());
        assert!(relevant(root, Path::new("/elsewhere/a.json")).is_none());
    }
}
