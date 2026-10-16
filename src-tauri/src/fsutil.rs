//! Small, dependency-free filesystem helpers shared by the commands: safe IDs,
//! bounded reads, atomic writes, containment checks and UTC stamps.

use std::fs;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

const RESERVED: [&str; 22] = [
    "con", "prn", "aux", "nul", "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9", "lpt1", "lpt2",
    "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
];

/// Same rule as the collector: `^[a-z0-9][a-z0-9_-]{0,63}$`, never a reserved Windows device name.
pub fn valid_id(value: &str) -> bool {
    let bytes = value.as_bytes();
    if bytes.is_empty() || bytes.len() > 64 {
        return false;
    }
    let first = bytes[0];
    if !(first.is_ascii_lowercase() || first.is_ascii_digit()) {
        return false;
    }
    if !bytes.iter().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || *b == b'_' || *b == b'-') {
        return false;
    }
    !RESERVED.contains(&value)
}

pub fn require_id(value: &str, label: &str) -> Result<(), String> {
    if valid_id(value) {
        Ok(())
    } else {
        Err(format!("{label} must be a safe lowercase ID (1-64 characters)"))
    }
}

/// Read a UTF-8 text file of at most `max` bytes (a BOM is dropped).
pub fn read_bounded(path: &Path, max: u64) -> Result<String, String> {
    let file = fs::File::open(path).map_err(|e| format!("{}: {e}", display_name(path)))?;
    let mut buffer = Vec::new();
    file.take(max + 1).read_to_end(&mut buffer).map_err(|e| format!("{}: {e}", display_name(path)))?;
    if buffer.len() as u64 > max {
        return Err(format!("{} is larger than {} KiB", display_name(path), max / 1024));
    }
    let text = String::from_utf8(buffer).map_err(|_| format!("{} is not valid UTF-8", display_name(path)))?;
    Ok(text.strip_prefix('\u{feff}').map(str::to_owned).unwrap_or(text))
}

/// Write through a temporary file in the same folder, then rename over the target.
pub fn atomic_write(path: &Path, contents: &[u8]) -> Result<(), String> {
    let parent = path.parent().ok_or("The target has no parent folder")?;
    fs::create_dir_all(parent).map_err(|e| e.to_string())?;
    let nonce = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_nanos()).unwrap_or(0);
    let temporary = parent.join(format!(".dashboard-{}-{nonce}.tmp", std::process::id()));
    let result = (|| -> std::io::Result<()> {
        let mut file = fs::File::create(&temporary)?;
        file.write_all(contents)?;
        file.sync_all()?;
        drop(file);
        fs::rename(&temporary, path)
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
    }
    result.map_err(|e| format!("Could not write {}: {e}", display_name(path)))
}

/// A relative path made only of normal components (no `..`, roots or prefixes).
pub fn plain_relative(relative: &str) -> Result<PathBuf, String> {
    let path = Path::new(relative);
    if relative.is_empty() || path.is_absolute() {
        return Err("Expected a relative path".into());
    }
    for component in path.components() {
        if !matches!(component, Component::Normal(_)) {
            return Err(format!("Path leaves its folder: {relative}"));
        }
    }
    Ok(path.to_path_buf())
}

/// Join `relative` under `root`, refusing traversal and any symbolic link or junction on the way.
pub fn contained(root: &Path, relative: &str) -> Result<PathBuf, String> {
    let relative = plain_relative(relative)?;
    let mut current = root.to_path_buf();
    for component in relative.components() {
        current.push(component);
        if let Ok(meta) = fs::symlink_metadata(&current) {
            if meta.file_type().is_symlink() || is_junction(&meta) {
                return Err(format!("Linked paths are not followed: {}", relative.display()));
            }
        }
    }
    Ok(current)
}

#[cfg(windows)]
fn is_junction(meta: &fs::Metadata) -> bool {
    use std::os::windows::fs::MetadataExt;
    const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
    meta.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
}

#[cfg(not(windows))]
fn is_junction(_meta: &fs::Metadata) -> bool {
    false
}

pub fn display_name(path: &Path) -> String {
    path.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_else(|| path.display().to_string())
}

/// `YYYYMMDDTHHMMSSZ` for the current UTC time (used for recoverable trash folders).
pub fn utc_stamp() -> String {
    let seconds = SystemTime::now().duration_since(UNIX_EPOCH).map(|d| d.as_secs() as i64).unwrap_or(0);
    let (days, rest) = (seconds.div_euclid(86_400), seconds.rem_euclid(86_400));
    let (year, month, day) = civil_from_days(days);
    format!("{year:04}{month:02}{day:02}T{:02}{:02}{:02}Z", rest / 3600, rest % 3600 / 60, rest % 60)
}

/// ISO 8601 UTC timestamp, second precision.
pub fn utc_iso() -> String {
    let stamp = utc_stamp();
    format!("{}-{}-{}T{}:{}:{}Z", &stamp[0..4], &stamp[4..6], &stamp[6..8], &stamp[9..11], &stamp[11..13], &stamp[13..15])
}

/// Howard Hinnant's days-to-civil conversion.
fn civil_from_days(days: i64) -> (i64, u32, u32) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = (doy - (153 * mp + 2) / 5 + 1) as u32;
    let month = if mp < 10 { mp + 3 } else { mp - 9 } as u32;
    (yoe + era * 400 + i64::from(month <= 2), month, day)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_follow_the_collector_rule() {
        assert!(valid_id("phd_thesis"));
        assert!(valid_id("a"));
        assert!(valid_id("2026-paper"));
        assert!(!valid_id(""));
        assert!(!valid_id("Paper"));
        assert!(!valid_id("-paper"));
        assert!(!valid_id("con"));
        assert!(!valid_id("a/b"));
        assert!(!valid_id(&"x".repeat(65)));
    }

    #[test]
    fn relative_paths_cannot_escape() {
        assert!(plain_relative("projects/a/project.json").is_ok());
        assert!(plain_relative("../x").is_err());
        assert!(plain_relative("a/../../x").is_err());
        assert!(plain_relative("").is_err());
        #[cfg(windows)]
        assert!(plain_relative("C:\\x").is_err());
        #[cfg(not(windows))]
        assert!(plain_relative("/etc/passwd").is_err());
    }

    #[test]
    fn atomic_write_replaces_and_reads_back() {
        let dir = tempfile::tempdir().unwrap();
        let target = dir.path().join("nested/file.json");
        atomic_write(&target, b"{\"a\":1}").unwrap();
        atomic_write(&target, b"{\"a\":2}").unwrap();
        assert_eq!(read_bounded(&target, 1024).unwrap(), "{\"a\":2}");
        assert!(read_bounded(&target, 3).is_err());
        let leftovers: Vec<_> = fs::read_dir(target.parent().unwrap()).unwrap().collect();
        assert_eq!(leftovers.len(), 1);
    }

    #[test]
    fn civil_dates_are_correct() {
        assert_eq!(civil_from_days(0), (1970, 1, 1));
        assert_eq!(civil_from_days(20_736), (2026, 10, 10));
        assert_eq!(civil_from_days(11_016), (2000, 2, 29));
        assert_eq!(utc_stamp().len(), 16);
    }
}
