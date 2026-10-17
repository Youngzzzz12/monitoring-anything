//! Runs the bundled Python collector with the bundled (or a system) Python: one-shot commands
//! (bounded output, timeout, no shell) and the long-running `watch` process whose
//! NDJSON stdout is forwarded to the UI as events.

use serde::Serialize;
use serde_json::Value;
use std::io::{BufRead, BufReader, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter, Manager};

pub const MAX_STDOUT: usize = 2 * 1024 * 1024;
const MAX_STDERR: usize = 64 * 1024;
const MAX_INPUT: usize = 1024 * 1024;
const MAX_LINE: usize = 64 * 1024;
const DEFAULT_TIMEOUT: u64 = 120;
const MAX_TIMEOUT: u64 = 3600;
/// Subcommands the UI may run. `watch` has its own lifecycle; `register` is advanced CLI-only.
const ALLOWED: [&str; 15] = [
    "info", "inspect", "connect", "collect", "status", "doctor", "projects", "create-project", "reconcile",
    "milestones", "profiles", "plan", "discover", "import-vault", "init-workspace",
];
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

#[derive(Serialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
pub struct CliOutput {
    pub code: Option<i32>,
    pub stdout: String,
    pub stderr: String,
    pub timed_out: bool,
    pub truncated: bool,
}

#[derive(Serialize, Clone, Debug)]
#[serde(rename_all = "camelCase")]
pub struct PythonCandidate {
    pub path: String,
    pub version: Option<String>,
    pub ok: bool,
    pub error: Option<String>,
}

#[derive(Serialize, Clone, Debug, Default)]
#[serde(rename_all = "camelCase")]
pub struct WatchStatus {
    pub running: bool,
    pub stopping: bool,
    pub error: Option<String>,
    pub started_at: Option<String>,
}

struct WatchHandle {
    id: u64,
    child: Arc<Mutex<Child>>,
    intentional: Arc<AtomicBool>,
    started_at: String,
}

#[derive(Default)]
pub struct CollectorState {
    watch: Mutex<Option<WatchHandle>>,
    last_error: Mutex<Option<String>>,
    sequence: AtomicU64,
}

/// Locate the collector scripts: $RD_COLLECTOR_DIR, the bundled resources, or the source tree in development.
pub fn collector_dir(app: &AppHandle) -> Result<PathBuf, String> {
    let mut candidates = Vec::new();
    if let Ok(value) = std::env::var("RD_COLLECTOR_DIR") {
        candidates.push(PathBuf::from(value));
    }
    if let Ok(resources) = app.path().resource_dir() {
        candidates.push(resources.join("collector"));
    }
    candidates.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("..").join("collector"));
    candidates
        .into_iter()
        .find(|dir| dir.join("tools").join("cli.py").is_file())
        .map(|dir| dir.canonicalize().map(strip_verbatim).unwrap_or(dir))
        .ok_or_else(|| "The bundled collector (collector/tools/cli.py) was not found. Reinstall the app.".to_string())
}

/// The Python runtime shipped with the app (scripts/fetch-python.mjs): the bundled resources, or the source tree in
/// development. `None` when the build has no runtime (then a system Python is used).
pub fn bundled_python(app: &AppHandle) -> Option<PathBuf> {
    let relative: &[&str] = if cfg!(windows) { &["python.exe"] } else { &["bin", "python3"] };
    let mut roots = Vec::new();
    if let Ok(resources) = app.path().resource_dir() {
        roots.push(resources.join("python"));
    }
    roots.push(PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("python"));
    roots.into_iter().map(|root| relative.iter().fold(root, |path, part| path.join(part))).find(|path| path.is_file()).map(strip_verbatim)
}

/// The OS time zone as an IANA name for the collector's day boundaries (a bundled Python has no zone database).
fn local_timezone() -> Option<String> {
    iana_time_zone::get_timezone().ok().filter(|name| !name.is_empty() && name.len() < 64)
}

fn strip_verbatim(path: PathBuf) -> PathBuf {
    let text = path.display().to_string();
    match text.strip_prefix(r"\\?\") {
        Some(rest) if !rest.starts_with("UNC\\") => PathBuf::from(rest),
        _ => path,
    }
}

/// A collector process: UTF-8 I/O, no window, piped output, and the OS time zone for day boundaries.
pub fn base_command(program: &Path) -> Command {
    let mut command = Command::new(program);
    command
        .env("PYTHONUTF8", "1")
        .env("PYTHONIOENCODING", "utf-8")
        .env("PYTHONDONTWRITEBYTECODE", "1")
        .env_remove("RD_LAYOUT")
        // Live updates stop when this process is gone (cli.py watch; see procjob.rs).
        .env("RD_PARENT_PID", std::process::id().to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    if let Some(zone) = local_timezone() {
        command.env("RD_TIMEZONE", zone);
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
}

/// Read up to `limit` bytes, then keep draining so the child never blocks on a full pipe.
pub fn read_capped<R: Read + Send + 'static>(mut reader: R, limit: usize) -> thread::JoinHandle<(Vec<u8>, bool)> {
    thread::spawn(move || {
        let mut kept = Vec::new();
        let mut buffer = [0u8; 16 * 1024];
        let mut truncated = false;
        loop {
            match reader.read(&mut buffer) {
                Ok(0) | Err(_) => break,
                Ok(n) => {
                    let room = limit.saturating_sub(kept.len());
                    if room < n {
                        truncated = true;
                    }
                    kept.extend_from_slice(&buffer[..n.min(room)]);
                }
            }
        }
        (kept, truncated)
    })
}

/// Run a program with argv (never through a shell), optional stdin, a timeout and capped output.
pub fn run_bounded(program: &Path, args: &[String], input: Option<&str>, timeout: Duration, cwd: Option<&Path>) -> Result<CliOutput, String> {
    let mut cmd = base_command(program);
    cmd.args(args);
    if let Some(dir) = cwd {
        cmd.current_dir(dir);
    }
    if input.is_some() {
        cmd.stdin(Stdio::piped());
    }
    let mut child = cmd.spawn().map_err(|e| format!("Python could not be started ({}): {e}", program.display()))?;
    crate::procjob::contain(&child);
    if let (Some(text), Some(mut stdin)) = (input, child.stdin.take()) {
        let payload = text.as_bytes().to_vec();
        thread::spawn(move || {
            let _ = stdin.write_all(&payload);
        });
    }
    let stdout = read_capped(child.stdout.take().expect("stdout piped"), MAX_STDOUT);
    let stderr = read_capped(child.stderr.take().expect("stderr piped"), MAX_STDERR);
    let started = Instant::now();
    let mut timed_out = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if started.elapsed() >= timeout => {
                timed_out = true;
                let _ = child.kill();
                break child.wait().ok();
            }
            Ok(None) => thread::sleep(Duration::from_millis(25)),
            Err(error) => return Err(error.to_string()),
        }
    };
    let (out, out_truncated) = stdout.join().unwrap_or_default();
    let (err, _) = stderr.join().unwrap_or_default();
    Ok(CliOutput {
        code: if timed_out { None } else { status.and_then(|s| s.code()) },
        stdout: String::from_utf8_lossy(&out).into_owned(),
        stderr: String::from_utf8_lossy(&err).into_owned(),
        timed_out,
        truncated: out_truncated,
    })
}

fn probe(path: &Path, prefix: &[&str]) -> PythonCandidate {
    let mut args: Vec<String> = prefix.iter().map(|s| s.to_string()).collect();
    args.push("-c".into());
    args.push("import sys, json; print(json.dumps({'v': '.'.join(map(str, sys.version_info[:3])), 'ok': sys.version_info >= (3, 10), 'exe': sys.executable}))".into());
    let display = path.display().to_string();
    match run_bounded(path, &args, None, Duration::from_secs(8), None) {
        Ok(output) if output.code == Some(0) => match serde_json::from_str::<Value>(output.stdout.trim()) {
            Ok(value) => {
                let version = value.get("v").and_then(Value::as_str).map(str::to_owned);
                let ok = value.get("ok").and_then(Value::as_bool).unwrap_or(false);
                // Keep the launchable path: a Microsoft Store alias works, its sys.executable target may not.
                let path = if prefix.is_empty() {
                    display
                } else {
                    value.get("exe").and_then(Value::as_str).filter(|s| !s.is_empty()).map(str::to_owned).unwrap_or(display)
                };
                let error = if ok { None } else { Some("Python 3.10 or newer is required".into()) };
                PythonCandidate { path, version, ok, error }
            }
            Err(_) => PythonCandidate { path: display, version: None, ok: false, error: Some("Unexpected interpreter output".into()) },
        },
        Ok(output) => PythonCandidate {
            path: display,
            version: None,
            ok: false,
            error: Some(output.stderr.trim().lines().last().unwrap_or("The interpreter did not run").to_owned()),
        },
        Err(error) => PythonCandidate { path: display, version: None, ok: false, error: Some(error) },
    }
}

/// Check one interpreter path (settings field).
pub fn check_python(path: &str) -> PythonCandidate {
    probe(Path::new(path), &[])
}

/// Find Python interpreters on PATH (python, python3, and the Windows `py -3` launcher).
pub fn detect_python() -> Vec<PythonCandidate> {
    let mut found: Vec<PythonCandidate> = Vec::new();
    let mut seen = std::collections::HashSet::new();
    // Microsoft Store app-execution aliases (WindowsApps) are probed last: they run a Store Python when one is
    // installed, and otherwise exit with an error instead of running anything.
    let mut aliases = Vec::new();
    for name in ["python3", "python"] {
        if let Ok(paths) = which::which_all(name) {
            for path in paths.take(6) {
                if path.display().to_string().to_lowercase().contains("windowsapps") {
                    aliases.push(path);
                    continue;
                }
                let candidate = probe(&path, &[]);
                if seen.insert(candidate.path.to_lowercase()) {
                    found.push(candidate);
                }
            }
        }
    }
    for path in aliases {
        let candidate = probe(&path, &[]);
        if candidate.ok && seen.insert(candidate.path.to_lowercase()) {
            found.push(candidate);
        }
    }
    if let Ok(launcher) = which::which("py") {
        let candidate = probe(&launcher, &["-3"]);
        if candidate.ok && seen.insert(candidate.path.to_lowercase()) {
            found.push(candidate);
        }
    }
    found.sort_by_key(|candidate| !candidate.ok);
    found
}

pub struct Runner {
    pub python: PathBuf,
    pub cli: PathBuf,
    pub workspace: PathBuf,
    /// The bundled runtime ignores PYTHON* variables and the user site folder, so a system install cannot leak in.
    pub isolated: bool,
}

impl Runner {
    pub fn args(&self, rest: &[String]) -> Vec<String> {
        let mut args: Vec<String> = if self.isolated { ["-E", "-s", "-B", "-X", "utf8"].iter().map(|s| s.to_string()).collect() } else { Vec::new() };
        args.extend(["-u".to_string(), self.cli.display().to_string(), "--workspace".into(), self.workspace.display().to_string()]);
        args.extend_from_slice(rest);
        args
    }
}

pub fn validate_request(command: &str, args: &[String], input: Option<&str>) -> Result<(), String> {
    if !ALLOWED.contains(&command) {
        return Err(format!("The collector command `{command}` is not available from the app"));
    }
    if command == "plan" && matches!(args.first().map(String::as_str), Some("propose") | Some("test")) {
        return Err("Agent runs go through agent_propose / agent_test, which add the stored key".into());
    }
    if args.len() > 64 || args.iter().any(|a| a.len() > 8192 || a.contains('\0')) {
        return Err("Collector arguments are too long".into());
    }
    if input.map(|i| i.len() > MAX_INPUT).unwrap_or(false) {
        return Err("Collector input is too large".into());
    }
    Ok(())
}

pub fn run(runner: &Runner, command: &str, args: &[String], input: Option<&str>, timeout_secs: Option<u64>) -> Result<CliOutput, String> {
    validate_request(command, args, input)?;
    let mut rest = vec![command.to_string()];
    rest.extend_from_slice(args);
    let timeout = Duration::from_secs(timeout_secs.unwrap_or(DEFAULT_TIMEOUT).clamp(1, MAX_TIMEOUT));
    run_bounded(&runner.python, &runner.args(&rest), input, timeout, Some(&runner.workspace))
}

impl CollectorState {
    pub fn status(&self) -> WatchStatus {
        let error = self.last_error.lock().ok().and_then(|e| e.clone());
        match self.watch.lock().ok().and_then(|w| w.as_ref().map(|h| (h.started_at.clone(), h.intentional.load(Ordering::SeqCst)))) {
            Some((started_at, stopping)) => WatchStatus { running: !stopping, stopping, error, started_at: Some(started_at) },
            None => WatchStatus { running: false, stopping: false, error, started_at: None },
        }
    }

    fn set_error(&self, error: Option<String>) {
        if let Ok(mut guard) = self.last_error.lock() {
            *guard = error;
        }
    }

    pub fn is_running(&self) -> bool {
        self.watch.lock().map(|w| w.is_some()).unwrap_or(false)
    }

    /// Start `cli.py watch`; snapshot events are emitted as `collector://event`, lifecycle as `collector://status`.
    pub fn start_watch(self: &Arc<Self>, app: &AppHandle, runner: &Runner) -> Result<WatchStatus, String> {
        let mut guard = self.watch.lock().map_err(|_| "Collector state is unavailable")?;
        if guard.is_some() {
            drop(guard);
            return Ok(self.status());
        }
        let mut cmd = base_command(&runner.python);
        cmd.args(runner.args(&["watch".into()])).current_dir(&runner.workspace);
        let mut child = cmd.spawn().map_err(|e| format!("Live updates could not start: {e}"))?;
        crate::procjob::contain(&child);
        let stdout = child.stdout.take().expect("stdout piped");
        let stderr = child.stderr.take().expect("stderr piped");
        let id = self.sequence.fetch_add(1, Ordering::SeqCst) + 1;
        let child = Arc::new(Mutex::new(child));
        let intentional = Arc::new(AtomicBool::new(false));
        *guard = Some(WatchHandle { id, child: child.clone(), intentional: intentional.clone(), started_at: crate::fsutil::utc_iso() });
        drop(guard);
        self.set_error(None);

        let events = app.clone();
        thread::spawn(move || {
            let mut reader = BufReader::new(stdout);
            let mut line = Vec::new();
            loop {
                line.clear();
                // Bounded line reads: an oversized line is dropped instead of growing the buffer.
                match (&mut reader).take(MAX_LINE as u64 + 1).read_until(b'\n', &mut line) {
                    Ok(0) | Err(_) => break,
                    Ok(_) => {
                        if line.len() > MAX_LINE {
                            let mut sink = Vec::new();
                            let _ = reader.read_until(b'\n', &mut sink);
                            continue;
                        }
                        if let Ok(value) = serde_json::from_slice::<Value>(&line) {
                            if value.get("event").and_then(Value::as_str).is_some() {
                                let _ = events.emit("collector://event", value);
                            }
                        }
                    }
                }
            }
        });
        let tail = Arc::new(Mutex::new(String::new()));
        let tail_writer = tail.clone();
        thread::spawn(move || {
            let reader = BufReader::new(stderr);
            for line in reader.lines().map_while(Result::ok) {
                if let Ok(mut text) = tail_writer.lock() {
                    text.push_str(&line);
                    text.push('\n');
                    if text.len() > 12_000 {
                        let cut = text.len() - 12_000;
                        let boundary = (cut..text.len()).find(|i| text.is_char_boundary(*i)).unwrap_or(text.len());
                        text.drain(..boundary);
                    }
                }
            }
        });
        let state = self.clone();
        let app_handle = app.clone();
        thread::spawn(move || {
            let code = loop {
                let polled = child.lock().ok().and_then(|mut c| c.try_wait().ok()).flatten();
                if let Some(status) = polled {
                    break status.code();
                }
                thread::sleep(Duration::from_millis(250));
            };
            let current = state.watch.lock().ok().map(|mut w| {
                if w.as_ref().map(|h| h.id) == Some(id) {
                    *w = None;
                    true
                } else {
                    false
                }
            });
            if current == Some(true) && !intentional.load(Ordering::SeqCst) {
                let detail = tail.lock().map(|t| t.trim().chars().rev().take(1500).collect::<String>().chars().rev().collect::<String>()).unwrap_or_default();
                state.set_error(Some(format!(
                    "Live updates stopped{}. {}",
                    code.map(|c| format!(" (exit {c})")).unwrap_or_default(),
                    detail
                )));
            }
            let _ = app_handle.emit("collector://status", state.status());
            crate::tray::sync(&app_handle);
        });
        let status = self.status();
        let _ = app.emit("collector://status", status.clone());
        crate::tray::sync(app);
        Ok(status)
    }

    /// Stop the watch process and wait (up to 10 s) for it to exit.
    pub fn stop_watch(&self, app: &AppHandle) -> Result<WatchStatus, String> {
        let handle = self.watch.lock().ok().and_then(|w| w.as_ref().map(|h| (h.id, h.child.clone(), h.intentional.clone())));
        let Some((id, child, intentional)) = handle else { return Ok(self.status()) };
        intentional.store(true, Ordering::SeqCst);
        let _ = app.emit("collector://status", self.status());
        if let Ok(mut process) = child.lock() {
            let _ = process.kill();
        }
        let deadline = Instant::now() + Duration::from_secs(10);
        while Instant::now() < deadline {
            let exited = child.lock().ok().and_then(|mut c| c.try_wait().ok()).flatten().is_some();
            if exited {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
        if let Ok(mut guard) = self.watch.lock() {
            if guard.as_ref().map(|h| h.id) == Some(id) {
                *guard = None;
            }
        }
        self.set_error(None);
        let status = self.status();
        let _ = app.emit("collector://status", status.clone());
        crate::tray::sync(app);
        Ok(status)
    }

    /// Kill without waiting (application exit).
    pub fn kill_now(&self) {
        if let Some(handle) = self.watch.lock().ok().and_then(|mut w| w.take()) {
            handle.intentional.store(true, Ordering::SeqCst);
            if let Ok(mut child) = handle.child.lock() {
                let _ = child.kill();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn requests_are_whitelisted_and_bounded() {
        assert!(validate_request("collect", &[], None).is_ok());
        assert!(validate_request("watch", &[], None).is_err());
        assert!(validate_request("register", &[], None).is_err());
        assert!(validate_request("plan", &["propose".into()], None).is_err());
        assert!(validate_request("plan", &["test".into()], None).is_err());
        assert!(validate_request("plan", &["detect".into()], None).is_ok());
        assert!(validate_request("plan", &["preview".into()], None).is_ok());
        assert!(validate_request("discover", &["--all".into()], None).is_ok());
        assert!(validate_request("collect", &["x\0".into()], None).is_err());
        assert!(validate_request("plan", &vec!["a".into(); 65], None).is_err());
        assert!(validate_request("plan", &[], Some(&"x".repeat(MAX_INPUT + 1))).is_err());
    }

    #[test]
    fn bounded_runs_capture_output_and_time_out() {
        let candidates = detect_python();
        let Some(python) = candidates.iter().find(|c| c.ok) else { return };
        let python = PathBuf::from(&python.path);
        let echo = run_bounded(&python, &["-c".into(), "import sys; print(sys.stdin.read().upper())".into()], Some("hi"), Duration::from_secs(20), None).unwrap();
        assert_eq!(echo.code, Some(0));
        assert_eq!(echo.stdout.trim(), "HI");
        let slow = run_bounded(&python, &["-c".into(), "import time; time.sleep(30)".into()], None, Duration::from_millis(500), None).unwrap();
        assert!(slow.timed_out);
        assert_eq!(slow.code, None);
        let big = run_bounded(&python, &["-c".into(), "print('x' * 3_000_000)".into()], None, Duration::from_secs(20), None).unwrap();
        assert!(big.truncated);
        assert_eq!(big.stdout.len(), MAX_STDOUT);
    }
}
