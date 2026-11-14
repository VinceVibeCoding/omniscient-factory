//! Commands for the OSD (usage snapshot, config, context menu) and the chat:
//! widget chats run headless by the daemon (/chats) and the transcript of a running
//! session. Inputs are validated here; the daemon re-validates everything.

use crate::client::validate_id;
use crate::commands::{daemon_for, get, post};
use crate::context_menu::{popup, put_config};
use crate::state::{lock, AppState};
use serde_json::{json, Value};
use tauri::{AppHandle, State, WebviewWindow};

type CmdResult<T> = Result<T, String>;

pub const CHAT_TEXT_MAX: usize = 20_000;
const PROJECT_MAX: usize = 100;
const TRANSCRIPT_DEFAULT: u32 = 50;
const TRANSCRIPT_MAX: u32 = 200;

/// A chat message: trimmed, not empty, at most CHAT_TEXT_MAX characters.
pub fn validate_text(text: &str) -> CmdResult<&str> {
    let text = text.trim();
    if text.is_empty() {
        return Err("Message vide.".into());
    }
    if text.chars().count() > CHAT_TEXT_MAX {
        return Err(format!("Message trop long ({CHAT_TEXT_MAX} caractères max)."));
    }
    Ok(text)
}

/// A project name from GET /projects: a name, never a path.
pub fn validate_project(name: &str) -> CmdResult<&str> {
    let name = name.trim();
    let bad = name.is_empty()
        || name.chars().count() > PROJECT_MAX
        || name.chars().any(|c| c.is_control() || c == '/' || c == '\\')
        || name == "."
        || name == "..";
    if bad {
        return Err("Projet invalide.".into());
    }
    Ok(name)
}

/// Transcript size: default 50, clamped to 1..=200.
pub fn transcript_limit(limit: Option<u32>) -> u32 {
    limit.unwrap_or(TRANSCRIPT_DEFAULT).clamp(1, TRANSCRIPT_MAX)
}

#[tauri::command]
pub async fn get_osd(state: State<'_, AppState>) -> CmdResult<Value> {
    get(&state, &["osd"]).await
}

#[tauri::command]
pub async fn get_config(state: State<'_, AppState>) -> CmdResult<Value> {
    let view = get(&state, &["config"]).await?;
    *lock(&state.config) = Some(view.clone());
    Ok(view)
}

#[tauri::command]
pub async fn set_config(state: State<'_, AppState>, patch: Value) -> CmdResult<Value> {
    let app_state: &AppState = &state;
    put_config(app_state, &patch).await
}

#[tauri::command]
pub async fn show_context_menu(app: AppHandle, window: WebviewWindow) -> CmdResult<()> {
    popup(app, window).await
}

#[tauri::command]
pub async fn list_projects(state: State<'_, AppState>) -> CmdResult<Value> {
    get(&state, &["projects"]).await
}

#[tauri::command]
pub async fn chat_create(state: State<'_, AppState>, project: String, text: String) -> CmdResult<Value> {
    let body = json!({ "project": validate_project(&project)?, "text": validate_text(&text)? });
    post(&state, &["chats"], &body).await
}

#[tauri::command]
pub async fn chat_send(state: State<'_, AppState>, chat_id: String, text: String) -> CmdResult<Value> {
    let body = json!({ "text": validate_text(&text)? });
    post(&state, &["chats", validate_id(&chat_id)?, "messages"], &body).await
}

#[tauri::command]
pub async fn chat_stop(state: State<'_, AppState>, chat_id: String) -> CmdResult<Value> {
    post(&state, &["chats", validate_id(&chat_id)?, "stop"], &json!({})).await
}

#[tauri::command]
pub async fn chat_list(state: State<'_, AppState>) -> CmdResult<Value> {
    get(&state, &["chats"]).await
}

#[tauri::command]
pub async fn chat_get(state: State<'_, AppState>, chat_id: String) -> CmdResult<Value> {
    get(&state, &["chats", validate_id(&chat_id)?]).await
}

#[tauri::command]
pub async fn session_transcript(state: State<'_, AppState>, id: String, limit: Option<u32>) -> CmdResult<Value> {
    let id = validate_id(&id)?;
    let limit = transcript_limit(limit).to_string();
    let app_state: &AppState = &state;
    let daemon = daemon_for(app_state)?;
    daemon
        .get_query(&["sessions", id, "transcript"], &[("limit", limit.as_str())])
        .await
        .map_err(|err| err.message())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn texts_are_trimmed_and_bounded() {
        assert_eq!(validate_text("  salut  ").unwrap(), "salut");
        assert!(validate_text("   ").is_err());
        assert!(validate_text(&"é".repeat(CHAT_TEXT_MAX)).is_ok());
        assert!(validate_text(&"é".repeat(CHAT_TEXT_MAX + 1)).is_err());
    }

    #[test]
    fn projects_are_names_not_paths() {
        assert_eq!(validate_project(" omniscient ").unwrap(), "omniscient");
        assert!(validate_project("").is_err());
        assert!(validate_project("../etc").is_err());
        assert!(validate_project("a\\b").is_err());
        assert!(validate_project("..").is_err());
        assert!(validate_project("a\nb").is_err());
        assert!(validate_project(&"p".repeat(PROJECT_MAX + 1)).is_err());
    }

    #[test]
    fn transcript_limit_is_clamped() {
        assert_eq!(transcript_limit(None), 50);
        assert_eq!(transcript_limit(Some(0)), 1);
        assert_eq!(transcript_limit(Some(10_000)), 200);
    }
}
