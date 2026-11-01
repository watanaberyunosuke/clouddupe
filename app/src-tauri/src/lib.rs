mod rclone;

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Arc, Mutex};

use clouddupe_core::{detect, parse_item, summarise, DetectOptions, FileEntry, Group, Summary};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tauri::{AppHandle, Emitter, State};

use rclone::{ChildSlot, ProviderInfo, RemoteInfo};

#[derive(Default)]
struct AppState {
    scan_slot: ChildSlot,
    scan_cancel: Arc<AtomicBool>,
    scanning: Arc<AtomicBool>,
    config_slot: ChildSlot,
    last: Arc<Mutex<Option<(Summary, Vec<Group>)>>>,
}

async fn blocking<T, F>(f: F) -> Result<T, String>
where
    T: Send + 'static,
    F: FnOnce() -> Result<T, String> + Send + 'static,
{
    tauri::async_runtime::spawn_blocking(f)
        .await
        .map_err(|e| format!("background task failed: {e}"))?
}

// --------------------------------------------------------------------------
// Drives
// --------------------------------------------------------------------------

#[tauri::command]
async fn rclone_version() -> Result<String, String> {
    blocking(rclone::version).await
}

#[tauri::command]
async fn list_remotes() -> Result<Vec<RemoteInfo>, String> {
    blocking(rclone::list_remotes).await
}

#[tauri::command]
async fn list_providers() -> Result<Vec<ProviderInfo>, String> {
    blocking(rclone::list_providers).await
}

#[tauri::command]
async fn delete_remote(name: String) -> Result<(), String> {
    validate_remote_name(&name)?;
    blocking(move || {
        let out = rclone::run(&["config", "delete", &name], None, None)?;
        if out.code == 0 {
            Ok(())
        } else {
            Err(rclone::error_message(&out.stderr, out.code))
        }
    })
    .await
}

/// rclone allows letters, digits and `_ - . + @` plus spaces, not leading
/// `-` or space. Rejecting a leading `-` also stops names being read as flags.
fn validate_remote_name(name: &str) -> Result<(), String> {
    let ok = !name.is_empty()
        && name.len() <= 64
        && !name.starts_with(['-', ' '])
        && !name.ends_with(' ')
        && name
            .chars()
            .all(|c| c.is_alphanumeric() || matches!(c, '_' | '-' | '.' | '+' | '@' | ' '));
    if ok {
        Ok(())
    } else {
        Err("Use letters, numbers, spaces and _ - . + @ only, not starting with - or a space.".into())
    }
}

// --------------------------------------------------------------------------
// Add-drive wizard: rclone's non-interactive config state machine
// --------------------------------------------------------------------------

#[derive(Debug, Serialize)]
struct Choice {
    value: String,
    help: String,
}

#[derive(Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct Question {
    name: String,
    help: String,
    default: String,
    examples: Vec<Choice>,
    required: bool,
    is_password: bool,
    kind: String,
    exclusive: bool,
}

#[derive(Debug, Serialize)]
struct ConfigStep {
    done: bool,
    state: String,
    question: Option<Question>,
    error: String,
    /// The next answer starts a browser sign-in that blocks until finished.
    oauth: bool,
}

fn parse_step(stdout: &str) -> Result<ConfigStep, String> {
    let trimmed = stdout.trim();
    if trimmed.is_empty() {
        return Ok(ConfigStep { done: true, state: String::new(), question: None, error: String::new(), oauth: false });
    }
    let v: Value = serde_json::from_str(trimmed).map_err(|e| format!("unexpected rclone output ({e})"))?;
    let state = v["State"].as_str().unwrap_or_default().to_string();
    let error = v["Error"].as_str().unwrap_or_default().to_string();
    if state.is_empty() {
        return Ok(ConfigStep { done: true, state, question: None, error, oauth: false });
    }
    let o = &v["Option"];
    let name = o["Name"].as_str().unwrap_or_default().to_string();
    let examples = o["Examples"]
        .as_array()
        .map(|a| {
            a.iter()
                .map(|e| Choice {
                    value: e["Value"].as_str().unwrap_or_default().to_string(),
                    help: e["Help"].as_str().unwrap_or_default().to_string(),
                })
                .collect()
        })
        .unwrap_or_default();
    let question = Question {
        name: name.clone(),
        help: o["Help"].as_str().unwrap_or_default().to_string(),
        default: o["DefaultStr"]
            .as_str()
            .map(str::to_string)
            .unwrap_or_else(|| value_to_string(&o["Default"])),
        examples,
        required: o["Required"].as_bool().unwrap_or(false),
        is_password: o["IsPassword"].as_bool().unwrap_or(false),
        kind: o["Type"].as_str().unwrap_or("string").to_string(),
        exclusive: o["Exclusive"].as_bool().unwrap_or(false),
    };
    Ok(ConfigStep { done: false, state, oauth: name == "config_is_local", question: Some(question), error })
}

fn value_to_string(v: &Value) -> String {
    match v {
        Value::String(s) => s.clone(),
        Value::Null => String::new(),
        other => other.to_string(),
    }
}

fn continue_config(name: &str, state: &str, result: &str, slot: &ChildSlot) -> Result<ConfigStep, String> {
    let out = rclone::run(
        &["config", "update", name, "--continue", "--non-interactive", "--all", "--state", state, "--result", result],
        None,
        Some(slot),
    )?;
    if out.code != 0 {
        return Err(rclone::error_message(&out.stderr, out.code));
    }
    parse_step(&out.stdout)
}

/// Skip questions with an obvious answer for a desktop app.
fn auto_answer(name: &str, mut step: ConfigStep, slot: &ChildSlot) -> Result<ConfigStep, String> {
    while let Some(q) = &step.question {
        if q.name != "config_fs_advanced" {
            break;
        }
        let state = step.state.clone();
        step = continue_config(name, &state, "false", slot)?;
    }
    Ok(step)
}

#[tauri::command]
async fn config_start(state: State<'_, AppState>, name: String, provider: String) -> Result<ConfigStep, String> {
    validate_remote_name(&name)?;
    if provider.is_empty() || !provider.chars().all(|c| c.is_ascii_alphanumeric() || c == '_') {
        return Err("invalid provider".into());
    }
    let slot = state.config_slot.clone();
    blocking(move || {
        if rclone::list_remotes()?.iter().any(|r| r.name == name) {
            return Err(format!("A drive called \"{name}\" already exists."));
        }
        let result = rclone::run(
            &["config", "create", &name, &provider, "--non-interactive", "--all"],
            None,
            Some(&slot),
        )
        .and_then(|out| {
            if out.code == 0 {
                parse_step(&out.stdout)
            } else {
                Err(rclone::error_message(&out.stderr, out.code))
            }
        })
        .and_then(|step| auto_answer(&name, step, &slot));
        if result.is_err() {
            // rclone saves the remote as soon as create starts; it did not exist
            // before (checked above), so remove the half-made entry.
            let _ = rclone::run(&["config", "delete", &name], None, None);
        }
        result
    })
    .await
}

#[tauri::command]
async fn config_answer(
    state: State<'_, AppState>,
    name: String,
    step_state: String,
    result: String,
    is_password: bool,
) -> Result<ConfigStep, String> {
    validate_remote_name(&name)?;
    let slot = state.config_slot.clone();
    blocking(move || {
        // rclone stores --continue passwords as given, so obscure first.
        // The password goes over stdin so it never appears in process lists.
        let answer = if is_password && !result.is_empty() {
            let out = rclone::run(&["obscure", "-"], Some(&result), None)?;
            if out.code != 0 {
                return Err(rclone::error_message(&out.stderr, out.code));
            }
            out.stdout.trim().to_string()
        } else {
            result
        };
        let step = continue_config(&name, &step_state, &answer, &slot)?;
        auto_answer(&name, step, &slot)
    })
    .await
}

#[tauri::command]
fn config_cancel(state: State<'_, AppState>) {
    rclone::kill(&state.config_slot);
}

// --------------------------------------------------------------------------
// Scanning
// --------------------------------------------------------------------------

#[derive(Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
struct ScanRequest {
    targets: Vec<String>,
    min_size: i64,
    size_only: bool,
    size_only_min: i64,
}

#[derive(Debug, Clone, Serialize)]
struct ScanProgress {
    target: String,
    files: usize,
    done: bool,
    error: Option<String>,
}

#[derive(Debug, Serialize)]
struct ScanResult {
    summary: Summary,
    groups: Vec<Group>,
}

struct ScanGuard(Arc<AtomicBool>);

impl Drop for ScanGuard {
    fn drop(&mut self) {
        self.0.store(false, Ordering::SeqCst);
    }
}

#[tauri::command]
async fn start_scan(app: AppHandle, state: State<'_, AppState>, request: ScanRequest) -> Result<ScanResult, String> {
    if request.targets.is_empty() {
        return Err("Choose at least one drive.".into());
    }
    for t in &request.targets {
        if t.starts_with('-') || !t.contains(':') {
            return Err(format!("invalid target: {t}"));
        }
    }
    if state.scanning.swap(true, Ordering::SeqCst) {
        return Err("A scan is already running.".into());
    }
    let guard = ScanGuard(state.scanning.clone());
    let slot = state.scan_slot.clone();
    let cancel = state.scan_cancel.clone();
    let last = state.last.clone();
    cancel.store(false, Ordering::SeqCst);

    blocking(move || {
        let _guard = guard;
        let mut files: Vec<FileEntry> = Vec::new();
        let mut errors: Vec<String> = Vec::new();

        for target in &request.targets {
            if cancel.load(Ordering::SeqCst) {
                return Err("cancelled".into());
            }
            let mut count = 0usize;
            let emit = |files: usize, done: bool, error: Option<String>| {
                let _ = app.emit(
                    "scan-progress",
                    ScanProgress { target: target.clone(), files, done, error },
                );
            };
            emit(0, false, None);
            let result = rclone::stream_listing(target, &slot, |item| {
                if let Some(entry) = parse_item(&item, target, files.len()) {
                    files.push(entry);
                    count += 1;
                    if count % 250 == 0 {
                        emit(count, false, None);
                    }
                }
            });
            match result {
                Ok(()) => emit(count, true, None),
                Err(e) if e == "cancelled" || cancel.load(Ordering::SeqCst) => {
                    return Err("cancelled".into())
                }
                Err(e) => {
                    emit(count, true, Some(e.clone()));
                    errors.push(e);
                }
            }
        }

        let opts = DetectOptions {
            min_size: request.min_size.max(0),
            size_only: request.size_only,
            size_only_min: request.size_only_min.max(0),
        };
        let groups = detect(&files, opts);
        let generated = chrono::Local::now().format("%Y-%m-%d %H:%M:%S %:z").to_string();
        let summary = summarise(&files, &groups, &errors, &generated);
        if let Ok(mut l) = last.lock() {
            *l = Some((summary.clone(), groups.clone()));
        }
        Ok(ScanResult { summary, groups })
    })
    .await
}

#[tauri::command]
fn cancel_scan(state: State<'_, AppState>) {
    state.scan_cancel.store(true, Ordering::SeqCst);
    rclone::kill(&state.scan_slot);
}

#[tauri::command]
async fn export_report(state: State<'_, AppState>, format: String, path: String) -> Result<(), String> {
    let last = state.last.clone();
    blocking(move || {
        let guard = last.lock().map_err(|_| "lock poisoned")?;
        let (summary, groups) = guard.as_ref().ok_or("Run a scan first.")?;
        let content = match format.as_str() {
            "html" => clouddupe_core::to_html(summary, groups),
            "csv" => clouddupe_core::to_csv(groups),
            "json" => clouddupe_core::to_json(summary, groups),
            other => return Err(format!("unknown format: {other}")),
        };
        std::fs::write(&path, content).map_err(|e| format!("could not write {path}: {e}"))
    })
    .await
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_dialog::init())
        .manage(AppState::default())
        .invoke_handler(tauri::generate_handler![
            rclone_version,
            list_remotes,
            list_providers,
            delete_remote,
            config_start,
            config_answer,
            config_cancel,
            start_scan,
            cancel_scan,
            export_report
        ])
        .run(tauri::generate_context!())
        .expect("error while running Cloud Dupe");
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn remote_names() {
        assert!(validate_remote_name("dropbox").is_ok());
        assert!(validate_remote_name("my drive 2").is_ok());
        assert!(validate_remote_name("-rf").is_err());
        assert!(validate_remote_name(" x").is_err());
        assert!(validate_remote_name("a/b").is_err());
        assert!(validate_remote_name("").is_err());
    }

    #[test]
    fn parses_question_and_finish() {
        let q = r#"{"State":"*all-set,1,false","Option":{"Name":"pass","Help":"Pikpak password.","Default":"","DefaultStr":"","Required":true,"IsPassword":true,"Exclusive":false,"Type":"string"},"Error":"","Result":""}"#;
        let step = parse_step(q).unwrap();
        assert!(!step.done);
        let question = step.question.unwrap();
        assert!(question.is_password && question.required);
        assert_eq!(question.name, "pass");

        let oauth = r#"{"State":"*oauth-islocal,,,","Option":{"Name":"config_is_local","Examples":[{"Value":"true","Help":"Yes"},{"Value":"false","Help":"No"}],"DefaultStr":"true","Type":"bool","Exclusive":true},"Error":""}"#;
        let step = parse_step(oauth).unwrap();
        assert!(step.oauth);
        assert_eq!(step.question.unwrap().examples.len(), 2);

        assert!(parse_step(r#"{"State":"","Option":null,"Error":"","Result":""}"#).unwrap().done);
        assert!(parse_step("").unwrap().done);
    }
}
