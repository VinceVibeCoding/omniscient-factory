//! Tauri commands called by the webview. Inputs are validated here (the boundary);
//! errors are French strings shown in the widget.

use crate::client::{validate_id, Daemon};
use crate::pairing::fetch_token;
use crate::settings::{self, Settings};
use crate::state::{lock, AppState, LinkStatus};
use crate::stream;
use crate::window::{set_mode, Mode};
use serde_json::{json, Map, Value};
use tauri::{AppHandle, Manager, State, WebviewWindow};

type CmdResult<T> = Result<T, String>;

const PROMPT_MAX: usize = 4000;
const ANSWERS_MAX: usize = 20;

fn daemon(state: &State<'_, AppState>) -> CmdResult<Daemon> {
    let settings = lock(&state.settings).clone();
    Daemon::new(&state.http, &settings)
}

async fn get(state: &State<'_, AppState>, segments: &[&str]) -> CmdResult<Value> {
    daemon(state)?.get(segments).await.map_err(|err| err.message())
}

async fn post(state: &State<'_, AppState>, segments: &[&str], body: &Value) -> CmdResult<Value> {
    daemon(state)?.post(segments, body).await.map_err(|err| err.message())
}

/// One of {"decision"}, {"answers"} or {"choice"} with allowed values only.
pub fn validate_answer(answer: &Value) -> CmdResult<Value> {
    let obj = answer.as_object().ok_or("Réponse invalide.")?;
    if let Some(decision) = obj.get("decision").and_then(Value::as_str) {
        if !matches!(decision, "allow" | "deny" | "always") {
            return Err("Décision inconnue.".into());
        }
        return Ok(json!({ "decision": decision }));
    }
    if let Some(choice) = obj.get("choice").and_then(Value::as_str) {
        if !matches!(choice, "raise" | "stop") {
            return Err("Choix inconnu.".into());
        }
        return Ok(json!({ "choice": choice }));
    }
    let answers = obj.get("answers").and_then(Value::as_object).ok_or("Réponse invalide.")?;
    if answers.is_empty() || answers.len() > ANSWERS_MAX {
        return Err("Réponse invalide.".into());
    }
    let mut clean = Map::new();
    for (question, label) in answers {
        let label = label.as_str().ok_or("Réponse invalide.")?;
        if label.trim().is_empty() || label.chars().count() > PROMPT_MAX {
            return Err("Réponse vide ou trop longue.".into());
        }
        clean.insert(question.clone(), Value::String(label.to_string()));
    }
    Ok(json!({ "answers": clean }))
}

#[tauri::command]
pub async fn list_sessions(state: State<'_, AppState>) -> CmdResult<Value> {
    get(&state, &["sessions"]).await
}

#[tauri::command]
pub async fn get_session(state: State<'_, AppState>, id: String) -> CmdResult<Value> {
    get(&state, &["sessions", validate_id(&id)?]).await
}

#[tauri::command]
pub async fn get_usage(state: State<'_, AppState>) -> CmdResult<Value> {
    get(&state, &["usage"]).await
}

#[tauri::command]
pub async fn get_theme(state: State<'_, AppState>) -> CmdResult<Value> {
    get(&state, &["theme"]).await
}

#[tauri::command]
pub async fn answer_pending(state: State<'_, AppState>, id: String, answer: Value) -> CmdResult<Value> {
    let body = validate_answer(&answer)?;
    post(&state, &["pending", validate_id(&id)?, "answer"], &body).await
}

#[tauri::command]
pub async fn dismiss_session(state: State<'_, AppState>, id: String) -> CmdResult<Value> {
    post(&state, &["sessions", validate_id(&id)?, "dismiss"], &json!({})).await
}

#[tauri::command]
pub async fn jump_session(state: State<'_, AppState>, id: String) -> CmdResult<Value> {
    post(&state, &["sessions", validate_id(&id)?, "jump"], &json!({})).await
}

#[tauri::command]
pub async fn prompt_session(state: State<'_, AppState>, id: String, text: String) -> CmdResult<Value> {
    let text = text.trim();
    if text.is_empty() || text.len() > PROMPT_MAX {
        return Err("Message vide ou trop long (4000 caractères max).".into());
    }
    post(&state, &["sessions", validate_id(&id)?, "prompt"], &json!({ "text": text })).await
}

#[tauri::command]
pub fn get_settings(state: State<'_, AppState>) -> Settings {
    lock(&state.settings).clone()
}

#[tauri::command]
pub fn link_status(state: State<'_, AppState>) -> LinkStatus {
    lock(&state.link).clone()
}

fn store(app: &AppHandle, settings: Settings) -> CmdResult<Settings> {
    let state = app.state::<AppState>();
    settings::save(&state.config_dir, &settings)?;
    *lock(&state.settings) = settings.clone();
    stream::restart(app);
    Ok(settings)
}

#[tauri::command]
pub fn save_settings(app: AppHandle, settings: Settings) -> CmdResult<Settings> {
    store(&app, settings.validated()?)
}

#[tauri::command]
pub async fn pair_via_wsl(app: AppHandle) -> CmdResult<Settings> {
    let current = lock(&app.state::<AppState>().settings).clone();
    let token = fetch_token(&current.wsl_distro, &current.token_command).await?;
    store(&app, current.with_token(&token)?)
}

#[tauri::command]
pub fn set_window_mode(app: AppHandle, window: WebviewWindow, mode: String) -> CmdResult<()> {
    let mode = Mode::parse(&mode)?;
    *lock(&app.state::<AppState>().mode) = mode;
    set_mode(&window, mode)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_known_answers() {
        assert_eq!(validate_answer(&json!({"decision": "always"})).unwrap(), json!({"decision": "always"}));
        assert_eq!(validate_answer(&json!({"choice": "stop"})).unwrap(), json!({"choice": "stop"}));
        let answers = json!({"answers": {"Quelle base ?": "Postgres"}});
        assert_eq!(validate_answer(&answers).unwrap(), answers);
    }

    #[test]
    fn rejects_unknown_or_empty_answers() {
        assert!(validate_answer(&json!({"decision": "maybe"})).is_err());
        assert!(validate_answer(&json!({"choice": "raise_a_lot"})).is_err());
        assert!(validate_answer(&json!({"answers": {}})).is_err());
        assert!(validate_answer(&json!({"answers": {"q": "  "}})).is_err());
        assert!(validate_answer(&json!("allow")).is_err());
    }

    #[test]
    fn strips_extra_fields() {
        let out = validate_answer(&json!({"decision": "deny", "message": "x", "evil": 1})).unwrap();
        assert_eq!(out, json!({"decision": "deny"}));
    }
}
