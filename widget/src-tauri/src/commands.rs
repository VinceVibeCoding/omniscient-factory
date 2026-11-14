//! Tauri commands called by the webview. Inputs are validated here (the boundary);
//! errors are French strings shown in the widget. The widget token never crosses into
//! the webview: get_settings has no token, pairing returns only messages.

use crate::client::{validate_id, Daemon};
use crate::settings::{self, Settings};
use crate::state::{is_verified, lock, AppState, LinkStatus};
use crate::stream;
use crate::window::{set_mode, Mode};
use crate::{pairing, vault};
use serde_json::{json, Map, Value};
use tauri::{AppHandle, Manager, State, WebviewWindow};

type CmdResult<T> = Result<T, String>;

const PROMPT_MAX: usize = 4000;
const ANSWERS_MAX: usize = 20;

/// A paired client, only once the stream loop has verified the daemon (M5).
pub(crate) fn daemon_for(state: &AppState) -> CmdResult<Daemon> {
    if !is_verified(state) {
        return Err("Démon pas encore vérifié : attendez la connexion (voir les réglages).".into());
    }
    let settings = lock(&state.settings).clone();
    let token = lock(&state.token).clone();
    Daemon::new(&state.http, &settings, &token)
}

fn daemon(state: &State<'_, AppState>) -> CmdResult<Daemon> {
    let app_state: &AppState = state;
    daemon_for(app_state)
}

pub(crate) async fn get(state: &State<'_, AppState>, segments: &[&str]) -> CmdResult<Value> {
    daemon(state)?.get(segments).await.map_err(|err| err.message())
}

pub(crate) async fn post(state: &State<'_, AppState>, segments: &[&str], body: &Value) -> CmdResult<Value> {
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

/// The settings shown in the webview: the daemon URL only (no token, ever).
#[tauri::command]
pub fn get_settings(state: State<'_, AppState>) -> Settings {
    let settings = lock(&state.settings).clone();
    settings
}

#[tauri::command]
pub fn link_status(state: State<'_, AppState>) -> LinkStatus {
    let link = lock(&state.link).clone();
    link
}

#[tauri::command]
pub fn save_settings(app: AppHandle, settings: Settings) -> CmdResult<Settings> {
    let settings = settings.validated()?;
    let state = app.state::<AppState>();
    settings::save(&state.config_dir, &settings)?;
    *lock(&state.settings) = settings.clone();
    stream::restart(&app);
    Ok(settings)
}

fn anonymous(app: &AppHandle) -> CmdResult<Daemon> {
    let state = app.state::<AppState>();
    let settings = lock(&state.settings).clone();
    let daemon = Daemon::anonymous(&state.http, &settings)?;
    Ok(daemon)
}

/// Asks the daemon to show a pairing code on the Windows desktop.
#[tauri::command]
pub async fn pair_start(app: AppHandle) -> CmdResult<String> {
    let daemon = anonymous(&app)?;
    let reply = daemon.post_public(&["pair", "start"], &json!({})).await.map_err(|err| err.message())?;
    let message = reply.get("message").and_then(Value::as_str).unwrap_or("Code affiché sur le bureau Windows.");
    Ok(message.to_string())
}

/// Sends the code; stores the returned widget token in the Windows vault only.
#[tauri::command]
pub async fn pair_finish(app: AppHandle, code: String) -> CmdResult<String> {
    let code = pairing::validate_code(&code)?;
    let daemon = anonymous(&app)?;
    let body = json!({ "code": code, "client_name": pairing::CLIENT_NAME });
    let reply = daemon.post_public(&["pair", "finish"], &body).await.map_err(|err| err.message())?;
    let raw = reply.get("token").and_then(Value::as_str).unwrap_or_default();
    let token = settings::validate_token(raw)?;
    vault::save(&token)?;
    let state = app.state::<AppState>();
    *lock(&state.token) = token;
    stream::restart(&app);
    Ok("Widget couplé.".into())
}

/// Forgets the widget token (here; `omniscient group pair --revoke` forgets it daemon-side).
#[tauri::command]
pub fn unpair(app: AppHandle) -> CmdResult<String> {
    vault::clear()?;
    let state = app.state::<AppState>();
    *lock(&state.token) = String::new();
    stream::restart(&app);
    Ok("Couplage oublié sur ce PC.".into())
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
