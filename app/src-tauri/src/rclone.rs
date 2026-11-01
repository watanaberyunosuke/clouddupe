//! Runs the rclone binary bundled next to the app executable.
//!
//! `std::process` is used instead of the shell plugin because password
//! obscuring needs stdin closed after writing, and long-running listings and
//! browser sign-ins need to be killable on cancel.

use std::io::{BufRead, BufReader, Read, Write};
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex};

use serde::Serialize;
use serde_json::Value;

/// Shared handle so a running process can be killed from another command.
pub type ChildSlot = Arc<Mutex<Option<Child>>>;

pub fn binary_path() -> Result<PathBuf, String> {
    let exe = std::env::current_exe().map_err(|e| format!("cannot locate app executable: {e}"))?;
    let dir = exe.parent().ok_or("cannot locate app directory")?;
    let name = if cfg!(windows) { "rclone.exe" } else { "rclone" };
    let path = dir.join(name);
    if path.exists() {
        Ok(path)
    } else {
        Err(format!(
            "bundled rclone not found at {}. Run `npm run fetch-rclone` before building.",
            path.display()
        ))
    }
}

fn command(args: &[&str]) -> Result<Command, String> {
    let mut cmd = Command::new(binary_path()?);
    cmd.args(args);
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        cmd.creation_flags(CREATE_NO_WINDOW);
    }
    Ok(cmd)
}

pub struct Output {
    pub code: i32,
    pub stdout: String,
    pub stderr: String,
}

/// Run rclone to completion. If `slot` is given the child can be killed via
/// [`kill`] while it runs (used for browser sign-in, which blocks).
pub fn run(args: &[&str], stdin: Option<&str>, slot: Option<&ChildSlot>) -> Result<Output, String> {
    let mut cmd = command(args)?;
    cmd.stdin(if stdin.is_some() { Stdio::piped() } else { Stdio::null() })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());
    let mut child = cmd.spawn().map_err(|e| format!("failed to start rclone: {e}"))?;

    if let Some(input) = stdin {
        // Dropping the handle closes stdin so rclone sees end of input.
        let mut pipe = child.stdin.take().ok_or("rclone stdin unavailable")?;
        pipe.write_all(input.as_bytes()).map_err(|e| format!("write to rclone failed: {e}"))?;
    }
    let mut out = child.stdout.take().ok_or("rclone stdout unavailable")?;
    let mut err = child.stderr.take().ok_or("rclone stderr unavailable")?;
    let mut local = Some(child);
    if let Some(s) = slot {
        *s.lock().map_err(|_| "lock poisoned")? = local.take();
    }

    let err_thread = std::thread::spawn(move || {
        let mut s = String::new();
        let _ = err.read_to_string(&mut s);
        s
    });
    let mut stdout = String::new();
    let _ = out.read_to_string(&mut stdout);
    let stderr = err_thread.join().unwrap_or_default();

    let status = match slot {
        Some(s) => {
            let child = s.lock().map_err(|_| "lock poisoned")?.take();
            match child {
                Some(mut c) => c.wait().map_err(|e| e.to_string())?,
                None => return Err("cancelled".into()),
            }
        }
        None => local
            .take()
            .ok_or("rclone process lost")?
            .wait()
            .map_err(|e| e.to_string())?,
    };
    Ok(Output { code: status.code().unwrap_or(-1), stdout, stderr })
}

/// Kill whatever is in the slot. The waiting side sees "cancelled".
pub fn kill(slot: &ChildSlot) {
    if let Ok(mut guard) = slot.lock() {
        if let Some(mut child) = guard.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// Most useful line from rclone's stderr for showing to the user.
pub fn error_message(stderr: &str, code: i32) -> String {
    let line = stderr
        .lines()
        .rev()
        .find(|l| l.contains("ERROR") || l.contains("Fatal error") || l.contains("Failed"))
        .or_else(|| stderr.lines().rev().find(|l| !l.trim().is_empty()))
        .unwrap_or("");
    let cleaned = line
        .split_once("Fatal error: ")
        .map(|(_, rest)| rest)
        .or_else(|| line.split_once("ERROR : ").map(|(_, rest)| rest))
        .unwrap_or(line)
        .trim();
    if cleaned.is_empty() {
        format!("rclone exited with code {code}")
    } else {
        cleaned.to_string()
    }
}

pub fn version() -> Result<String, String> {
    let out = run(&["version"], None, None)?;
    Ok(out.stdout.lines().next().unwrap_or("rclone").to_string())
}

#[derive(Debug, Clone, Serialize)]
pub struct RemoteInfo {
    pub name: String,
    #[serde(rename = "type")]
    pub kind: String,
    pub description: String,
}

pub fn list_remotes() -> Result<Vec<RemoteInfo>, String> {
    let out = run(&["listremotes", "--json"], None, None)?;
    if out.code != 0 {
        return Err(error_message(&out.stderr, out.code));
    }
    let items: Vec<Value> = serde_json::from_str(out.stdout.trim()).unwrap_or_default();
    Ok(items
        .iter()
        .map(|v| RemoteInfo {
            name: v["name"].as_str().unwrap_or_default().to_string(),
            kind: v["type"].as_str().unwrap_or_default().to_string(),
            description: v["description"].as_str().unwrap_or_default().to_string(),
        })
        .filter(|r| !r.name.is_empty())
        .collect())
}

#[derive(Debug, Clone, Serialize)]
pub struct ProviderInfo {
    pub name: String,
    pub description: String,
}

/// Wrapper backends that need another remote underneath are not offered.
const HIDDEN_PROVIDERS: &[&str] = &[
    "alias", "archive", "cache", "chunker", "combine", "compress", "crypt", "hasher", "memory", "union",
];

pub fn list_providers() -> Result<Vec<ProviderInfo>, String> {
    let out = run(&["config", "providers"], None, None)?;
    if out.code != 0 {
        return Err(error_message(&out.stderr, out.code));
    }
    let items: Vec<Value> = serde_json::from_str(&out.stdout).map_err(|e| e.to_string())?;
    let mut providers: Vec<ProviderInfo> = items
        .iter()
        .filter(|v| v["Hide"].as_i64().unwrap_or(0) == 0)
        .filter_map(|v| {
            let name = v["Name"].as_str()?.to_string();
            if HIDDEN_PROVIDERS.contains(&name.as_str()) {
                return None;
            }
            Some(ProviderInfo { name, description: v["Description"].as_str().unwrap_or_default().to_string() })
        })
        .collect();
    providers.sort_by(|a, b| a.description.to_lowercase().cmp(&b.description.to_lowercase()));
    Ok(providers)
}

/// Stream `rclone lsjson` for one target, calling `on_item` per file item.
/// Returns the stderr tail on failure. Killing the slot ends the stream.
pub fn stream_listing(
    target: &str,
    slot: &ChildSlot,
    mut on_item: impl FnMut(Value),
) -> Result<(), String> {
    let mut cmd = command(&["lsjson", "-R", "--files-only", "--hash", "--no-mimetype", target])?;
    cmd.stdin(Stdio::null()).stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = cmd.spawn().map_err(|e| format!("failed to start rclone: {e}"))?;
    let out = child.stdout.take().ok_or("rclone stdout unavailable")?;
    let mut err = child.stderr.take().ok_or("rclone stderr unavailable")?;
    *slot.lock().map_err(|_| "lock poisoned")? = Some(child);

    let err_thread = std::thread::spawn(move || {
        let mut s = String::new();
        let _ = err.read_to_string(&mut s);
        s
    });
    let cancelled = || slot.lock().map(|g| g.is_none()).unwrap_or(true);
    for line in BufReader::new(out).lines() {
        let Ok(line) = line else {
            break;
        };
        let line = line.trim();
        if line.is_empty() || line == "[" || line == "]" {
            continue;
        }
        let line = line.strip_suffix(',').unwrap_or(line);
        match serde_json::from_str::<Value>(line) {
            Ok(item) => on_item(item),
            // A kill mid-line leaves a truncated record; report that as a cancel.
            Err(_) if cancelled() => return Err("cancelled".into()),
            Err(e) => {
                kill(slot);
                return Err(format!("{target}: could not parse rclone output ({e})"));
            }
        }
    }
    let stderr = err_thread.join().unwrap_or_default();

    let child = slot.lock().map_err(|_| "lock poisoned")?.take();
    let Some(mut child) = child else {
        return Err("cancelled".into());
    };
    let status = child.wait().map_err(|e| e.to_string())?;
    if status.success() {
        Ok(())
    } else {
        Err(format!("{target}: {}", error_message(&stderr, status.code().unwrap_or(-1))))
    }
}
