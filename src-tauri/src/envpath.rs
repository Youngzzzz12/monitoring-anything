//! The PATH that child processes (the collector, agent CLIs such as Claude Code or Codex) see.
//!
//! An app started from the Dock, Finder or Explorer does not get the PATH a terminal has. On macOS and Linux a GUI app
//! gets a minimal launchd PATH (no Homebrew, npm, ~/.local/bin), so installed agents look missing and a Node-based CLI
//! cannot find `node`. On Windows an app started from Explorer keeps the PATH from sign-in and misses tools installed
//! since. At start the app widens its own PATH, which every child inherits: first the login shell's PATH (macOS, Linux)
//! or the current registry PATH (Windows), then the usual install folders of agent CLIs that exist on this computer.

use std::path::{Path, PathBuf};

#[cfg(windows)]
const SEPARATOR: char = ';';
#[cfg(not(windows))]
const SEPARATOR: char = ':';

/// Widen this process's PATH (call once, first thing at start, before other threads exist).
pub fn widen() {
    let current = std::env::var("PATH").unwrap_or_default();
    let mut extra: Vec<String> = system_path().map(|path| split(&path)).unwrap_or_default();
    extra.extend(common_dirs().into_iter().filter(|dir| dir.is_dir()).map(|dir| dir.display().to_string()));
    let merged = merge(&current, &extra);
    if merged != current {
        std::env::set_var("PATH", merged);
    }
}

fn split(path: &str) -> Vec<String> {
    path.split(SEPARATOR).map(str::trim).filter(|part| !part.is_empty()).map(str::to_owned).collect()
}

/// The current PATH followed by the extra folders it does not already list (case-insensitive on Windows).
pub fn merge(current: &str, extra: &[String]) -> String {
    let key = |part: &str| if cfg!(windows) { part.trim_end_matches(['\\', '/']).to_lowercase() } else { part.trim_end_matches('/').to_owned() };
    let mut parts = split(current);
    let mut seen: std::collections::HashSet<String> = parts.iter().map(|part| key(part)).collect();
    for part in extra {
        if seen.insert(key(part)) {
            parts.push(part.clone());
        }
    }
    parts.join(&SEPARATOR.to_string())
}

fn home() -> Option<PathBuf> {
    std::env::var_os(if cfg!(windows) { "USERPROFILE" } else { "HOME" }).map(PathBuf::from)
}

/// Folders agent CLIs and their runtimes are commonly installed into.
fn common_dirs() -> Vec<PathBuf> {
    let mut dirs = Vec::new();
    let Some(home) = home() else { return dirs };
    if cfg!(windows) {
        let env = |name: &str| std::env::var_os(name).map(PathBuf::from);
        if let Some(appdata) = env("APPDATA") {
            dirs.push(appdata.join("npm"));
        }
        if let Some(local) = env("LOCALAPPDATA") {
            dirs.push(local.join("Microsoft").join("WinGet").join("Links"));
            dirs.push(local.join("Volta").join("bin"));
            dirs.push(local.join("pnpm"));
        }
        if let Some(programs) = env("ProgramFiles") {
            dirs.push(programs.join("nodejs"));
        }
        for relative in [".local/bin", ".claude/local", "scoop/shims", ".bun/bin", ".cargo/bin"] {
            dirs.push(home.join(relative));
        }
    } else {
        for absolute in ["/opt/homebrew/bin", "/opt/homebrew/sbin", "/usr/local/bin", "/home/linuxbrew/.linuxbrew/bin"] {
            dirs.push(PathBuf::from(absolute));
        }
        for relative in [".local/bin", ".claude/local", ".npm-global/bin", ".bun/bin", ".volta/bin", ".cargo/bin",
                         "Library/pnpm", ".local/share/pnpm", ".yarn/bin", "n/bin"] {
            dirs.push(home.join(relative));
        }
        dirs.extend(nvm_bins(&home.join(".nvm").join("versions").join("node")));
    }
    dirs
}

/// nvm's Node versions, newest first (by folder name: v22.10.0 sorts after v20.18.1).
fn nvm_bins(root: &Path) -> Vec<PathBuf> {
    let Ok(entries) = std::fs::read_dir(root) else { return Vec::new() };
    let mut versions: Vec<(Vec<u64>, PathBuf)> = entries.flatten().filter_map(|entry| {
        let name = entry.file_name().to_string_lossy().trim_start_matches('v').to_owned();
        let numbers: Vec<u64> = name.split('.').map(|part| part.parse().ok()).collect::<Option<_>>()?;
        Some((numbers, entry.path().join("bin")))
    }).collect();
    versions.sort_by(|a, b| b.0.cmp(&a.0));
    versions.into_iter().map(|(_, bin)| bin).collect()
}

/// The PATH a terminal would have: the login shell's (macOS, Linux), bounded to a few seconds.
#[cfg(not(windows))]
fn system_path() -> Option<String> {
    use std::process::{Command, Stdio};
    use std::time::{Duration, Instant};
    let shell = std::env::var("SHELL").ok().filter(|shell| !shell.is_empty()).unwrap_or_else(|| "/bin/zsh".into());
    let script = if shell.ends_with("fish") { "printf '__MA_PATH__%s__MA_PATH__' (string join : $PATH)" } else { "printf '__MA_PATH__%s__MA_PATH__' \"$PATH\"" };
    let mut child = Command::new(&shell).args(["-l", "-i", "-c", script]).stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::null()).spawn().ok()?;
    let started = Instant::now();
    loop {
        match child.try_wait() {
            Ok(Some(_)) => break,
            Ok(None) if started.elapsed() < Duration::from_secs(4) => std::thread::sleep(Duration::from_millis(25)),
            _ => {
                let _ = child.kill();
                let _ = child.wait();
                return None;
            }
        }
    }
    let mut output = String::new();
    std::io::Read::read_to_string(child.stdout.as_mut()?, &mut output).ok()?;
    let start = output.find("__MA_PATH__")? + "__MA_PATH__".len();
    let end = output[start..].find("__MA_PATH__")? + start;
    Some(output[start..end].to_owned()).filter(|path| !path.is_empty())
}

/// The PATH as the registry holds it now (machine, then user), with %VARIABLES% expanded.
#[cfg(windows)]
fn system_path() -> Option<String> {
    use winreg::enums::{HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE};
    use winreg::RegKey;
    let read = |hive, key: &str| RegKey::predef(hive).open_subkey(key).ok()?.get_value::<String, _>("Path").ok();
    let machine = read(HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment");
    let user = read(HKEY_CURRENT_USER, "Environment");
    let joined = [machine, user].into_iter().flatten().map(|part| expand(&part)).collect::<Vec<_>>().join(";");
    Some(joined).filter(|path| !path.is_empty())
}

/// Expand %NAME% references with this process's environment (unknown names stay as written).
#[cfg_attr(not(windows), allow(dead_code))]
pub fn expand(text: &str) -> String {
    let mut out = String::new();
    let mut rest = text;
    while let Some(start) = rest.find('%') {
        out.push_str(&rest[..start]);
        let after = &rest[start + 1..];
        match after.find('%') {
            Some(end) if end > 0 => {
                let name = &after[..end];
                match std::env::var(name) {
                    Ok(value) => out.push_str(&value),
                    Err(_) => { out.push('%'); out.push_str(name); out.push('%'); }
                }
                rest = &after[end + 1..];
            }
            _ => { out.push('%'); rest = after; }
        }
    }
    out.push_str(rest);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn merge_appends_only_missing_folders() {
        let sep = SEPARATOR.to_string();
        let current = ["/usr/bin", "/bin"].join(&sep);
        let merged = merge(&current, &["/bin".into(), "/opt/homebrew/bin".into(), "/usr/bin/".into()]);
        assert_eq!(merged, ["/usr/bin", "/bin", "/opt/homebrew/bin"].join(&sep));
        assert_eq!(merge("", &["/a".into()]), "/a");
    }

    #[test]
    fn expand_replaces_known_variables_only() {
        std::env::set_var("MA_TEST_HOME", "C:\\Users\\me");
        assert_eq!(expand("%MA_TEST_HOME%\\AppData\\Roaming\\npm"), "C:\\Users\\me\\AppData\\Roaming\\npm");
        assert_eq!(expand("%NO_SUCH_VARIABLE_MA%\\x;50%"), "%NO_SUCH_VARIABLE_MA%\\x;50%");
    }

    #[cfg(windows)]
    #[test]
    fn the_registry_path_is_read_and_expanded() {
        let path = system_path().expect("the registry has a PATH");
        assert!(path.to_lowercase().contains("system32"), "{path}");
        assert!(!path.contains("%SystemRoot%"), "{path}");
    }

    #[test]
    fn nvm_versions_newest_first() {
        let dir = tempfile::tempdir().unwrap();
        for version in ["v20.18.1", "v22.10.0", "v9.0.0", "system"] {
            std::fs::create_dir_all(dir.path().join(version).join("bin")).unwrap();
        }
        let names: Vec<String> = nvm_bins(dir.path()).iter().map(|bin| bin.parent().unwrap().file_name().unwrap().to_string_lossy().into_owned()).collect();
        assert_eq!(names, ["v22.10.0", "v20.18.1", "v9.0.0"]);
    }
}
