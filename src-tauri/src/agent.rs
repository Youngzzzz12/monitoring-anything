//! Agent-designed monitoring plans (`plan propose`) and the API keys they need.
//!
//! Keys live in the OS credential store (Windows Credential Manager, macOS Keychain, the Secret Service on Linux),
//! never in settings, the workspace, logs or the web page: the page sends the provider without a key, this module adds
//! it from the store, passes the request to the collector on stdin and redacts the key from anything it returns.
//! A run can be stopped from the plan card; stopping ends the collector and the agent it started.

use serde_json::Value;
use std::collections::HashMap;
use std::io::Write;
use std::path::Path;
use std::process::Stdio;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use crate::collector::{self, CliOutput, Runner};

const SERVICE: &str = "io.github.monitoring-anything.agent";
const MAX_KEY: usize = 4096;
const MAX_REQUEST: usize = 1024 * 1024;
const DEFAULT_TIMEOUT: u64 = 600;
const MAX_TIMEOUT: u64 = 3600;

#[derive(Default)]
pub struct AgentState {
    runs: Mutex<HashMap<String, Arc<AtomicBool>>>,
}

fn require_provider_id(value: &str) -> Result<(), String> {
    let ok = !value.is_empty() && value.len() <= 64 && value.chars().all(|c| c.is_ascii_lowercase() || c.is_ascii_digit() || c == '-' || c == '_');
    if ok { Ok(()) } else { Err("Choose a valid agent provider".into()) }
}

fn entry(provider_id: &str) -> Result<keyring::Entry, String> {
    require_provider_id(provider_id)?;
    keyring::Entry::new(SERVICE, provider_id).map_err(|e| format!("The system credential store is unavailable: {e}"))
}

/// The stored key, or None (no key, or no credential store on this system).
pub fn get_key(provider_id: &str) -> Option<String> {
    entry(provider_id).ok()?.get_password().ok().filter(|key| !key.is_empty())
}

pub fn has_key(provider_id: &str) -> Result<bool, String> {
    let entry = entry(provider_id)?;
    match entry.get_password() {
        Ok(key) => Ok(!key.is_empty()),
        Err(keyring::Error::NoEntry) => Ok(false),
        Err(error) => Err(format!("The system credential store could not be read: {error}")),
    }
}

/// Store a key (trimmed), or delete it when empty.
pub fn set_key(provider_id: &str, value: Option<&str>) -> Result<bool, String> {
    let entry = entry(provider_id)?;
    let key = value.map(str::trim).unwrap_or("");
    if key.len() > MAX_KEY || key.contains(['\r', '\n', '\0']) {
        return Err("That does not look like an API key".into());
    }
    if key.is_empty() {
        return match entry.delete_credential() {
            Ok(()) | Err(keyring::Error::NoEntry) => Ok(false),
            Err(error) => Err(format!("The key could not be removed from the system credential store: {error}")),
        };
    }
    entry.set_password(key).map_err(|e| format!("The key could not be saved in the system credential store: {e}"))?;
    Ok(true)
}

/// Replace every occurrence of a secret in text shown to the user.
pub fn redact(text: &str, secret: Option<&str>) -> String {
    match secret {
        Some(secret) if secret.len() >= 4 => text.replace(secret, "••••"),
        _ => text.to_string(),
    }
}

/// Validate the page's request and add the stored key: (request JSON for stdin, key used, timeout seconds).
pub fn prepare(request: &Value, needs_source: bool) -> Result<(String, Option<String>, u64), String> {
    let mut request = request.clone();
    let object = request.as_object_mut().ok_or("The agent request must be an object")?;
    let source = object.get("source").and_then(Value::as_str).unwrap_or("");
    if needs_source && !Path::new(source).is_absolute() {
        return Err("Choose a project folder first".into());
    }
    let provider = object.get_mut("provider").and_then(Value::as_object_mut).ok_or("Choose an agent provider in Settings → Agent")?;
    // A key never comes from the page; only the credential store supplies one.
    provider.remove("apiKey");
    provider.remove("apiKeyEnv");
    let id = provider.get("id").and_then(Value::as_str).unwrap_or("").to_string();
    require_provider_id(&id)?;
    let key = get_key(&id);
    if let Some(key) = &key {
        provider.insert("apiKey".into(), Value::String(key.clone()));
    }
    let timeout = object.get("timeoutSeconds").and_then(Value::as_u64).unwrap_or(DEFAULT_TIMEOUT).clamp(30, MAX_TIMEOUT);
    let text = serde_json::to_string(&request).map_err(|e| e.to_string())?;
    if text.len() > MAX_REQUEST {
        return Err("The agent request is too large".into());
    }
    Ok((text, key, timeout))
}

/// End a process and everything it started (the agent CLI runs as the collector's child).
fn kill_tree(child: &mut std::process::Child) {
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        let _ = std::process::Command::new("taskkill")
            .args(["/F", "/T", "/PID", &child.id().to_string()])
            .stdin(Stdio::null()).stdout(Stdio::null()).stderr(Stdio::null())
            .creation_flags(0x0800_0000)
            .status();
    }
    #[cfg(unix)]
    {
        // The collector runs in its own process group (see `propose`), so the group is signalled.
        let _ = std::process::Command::new("kill").args(["-KILL", &format!("-{}", child.id())]).status();
    }
    let _ = child.kill();
}

impl AgentState {
    /// Run `plan propose` (or `plan test`, the connection check); `cancel(run_id)` stops it. Output is capped like any
    /// collector run and the key is redacted.
    pub fn run_action(&self, runner: &Runner, action: &str, run_id: &str, request: &Value) -> Result<CliOutput, String> {
        if !["propose", "test"].contains(&action) {
            return Err("Unknown agent action".into());
        }
        if run_id.is_empty() || run_id.len() > 64 || !run_id.chars().all(|c| c.is_ascii_alphanumeric() || c == '-') {
            return Err("Invalid agent run".into());
        }
        let (input, key, timeout) = prepare(request, action == "propose")?;
        let cancelled = Arc::new(AtomicBool::new(false));
        {
            let mut runs = self.runs.lock().map_err(|_| "Agent state is unavailable")?;
            if runs.contains_key(run_id) {
                return Err("This agent run is already in progress".into());
            }
            runs.insert(run_id.to_string(), cancelled.clone());
        }
        let result = run(runner, action, &input, Duration::from_secs(timeout + 60), &cancelled);
        if let Ok(mut runs) = self.runs.lock() {
            runs.remove(run_id);
        }
        let mut output = result?;
        if cancelled.load(Ordering::SeqCst) {
            return Err(if action == "test" { "The connection test was stopped.".into() } else { "The agent was stopped before it returned a plan.".into() });
        }
        output.stdout = redact(&output.stdout, key.as_deref());
        output.stderr = redact(&output.stderr, key.as_deref());
        Ok(output)
    }

    pub fn cancel(&self, run_id: &str) -> bool {
        self.runs.lock().ok().and_then(|runs| runs.get(run_id).cloned()).map(|flag| flag.store(true, Ordering::SeqCst)).is_some()
    }

    /// Stop every run (application exit).
    pub fn cancel_all(&self) {
        if let Ok(runs) = self.runs.lock() {
            for flag in runs.values() {
                flag.store(true, Ordering::SeqCst);
            }
        }
    }
}

fn run(runner: &Runner, action: &str, input: &str, timeout: Duration, cancelled: &AtomicBool) -> Result<CliOutput, String> {
    let args = runner.args(&["plan".into(), action.into()]);
    let mut command = collector::base_command(&runner.python);
    command.args(&args).current_dir(&runner.workspace).stdin(Stdio::piped());
    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }
    let mut child = command.spawn().map_err(|e| format!("Python could not be started ({}): {e}", runner.python.display()))?;
    crate::procjob::contain(&child);
    if let Some(mut stdin) = child.stdin.take() {
        let payload = input.as_bytes().to_vec();
        thread::spawn(move || {
            let _ = stdin.write_all(&payload);
        });
    }
    let stdout = collector::read_capped(child.stdout.take().expect("stdout piped"), collector::MAX_STDOUT);
    let stderr = collector::read_capped(child.stderr.take().expect("stderr piped"), 64 * 1024);
    let started = Instant::now();
    let mut timed_out = false;
    let status = loop {
        match child.try_wait() {
            Ok(Some(status)) => break Some(status),
            Ok(None) if cancelled.load(Ordering::SeqCst) || started.elapsed() >= timeout => {
                timed_out = !cancelled.load(Ordering::SeqCst);
                kill_tree(&mut child);
                break child.wait().ok();
            }
            Ok(None) => thread::sleep(Duration::from_millis(50)),
            Err(error) => return Err(error.to_string()),
        }
    };
    let (out, truncated) = stdout.join().unwrap_or_default();
    let (err, _) = stderr.join().unwrap_or_default();
    Ok(CliOutput {
        code: if timed_out || cancelled.load(Ordering::SeqCst) { None } else { status.and_then(|s| s.code()) },
        stdout: String::from_utf8_lossy(&out).into_owned(),
        stderr: String::from_utf8_lossy(&err).into_owned(),
        timed_out,
        truncated,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn requests_drop_page_keys_and_bound_the_timeout() {
        let request = json!({"source": if cfg!(windows) { "C:\\paper" } else { "/paper" }, "timeoutSeconds": 99999,
            "provider": {"id": "zz-test-provider-without-key", "kind": "http", "apiKey": "page-key", "apiKeyEnv": "HOME"}});
        let (text, key, timeout) = prepare(&request, true).unwrap();
        let parsed: Value = serde_json::from_str(&text).unwrap();
        assert!(key.is_none());
        assert!(parsed["provider"].get("apiKey").is_none());
        assert!(parsed["provider"].get("apiKeyEnv").is_none());
        assert_eq!(timeout, MAX_TIMEOUT);
        assert!(prepare(&json!({"source": "relative", "provider": {"id": "x"}}), true).is_err());
        assert!(prepare(&json!({"provider": {"id": "x"}}), false).is_ok());
        assert!(prepare(&json!({"source": if cfg!(windows) { "C:\\p" } else { "/p" }, "provider": {"id": "../x"}}), true).is_err());
    }

    #[test]
    fn keys_are_redacted_and_ids_checked() {
        assert_eq!(redact("error: key sk-abcdef rejected", Some("sk-abcdef")), "error: key •••• rejected");
        assert_eq!(redact("short", Some("abc")), "short");
        assert!(require_provider_id("claude-code").is_ok());
        assert!(require_provider_id("Claude Code").is_err());
        assert!(set_key("ok-id", Some("line\nbreak")).is_err());
    }
}
