//! Background SSE loop: GET /stream → Tauri events, with reconnect/backoff and a native
//! notification when a session becomes "waiting".

use crate::client::{Daemon, DaemonError};
use crate::settings::Settings;
use crate::sse::{SseEvent, SseParser};
use crate::state::{lock, set_link, AppState, LinkStatus};
use futures_util::StreamExt;
use reqwest::Client;
use serde_json::{json, Value};
use std::collections::HashMap;
use std::time::Duration;
use tauri::{AppHandle, Emitter, Manager};
use tauri_plugin_notification::NotificationExt;

pub const STREAM_EVENT: &str = "group://event";
const MIN_BACKOFF: Duration = Duration::from_secs(1);
const MAX_BACKOFF: Duration = Duration::from_secs(30);

/// Remembers each session's last state so we notify only on the transition to "waiting".
#[derive(Default)]
pub struct Notifier {
    states: HashMap<String, String>,
}

impl Notifier {
    /// Returns the session title when this "session" event is a transition to waiting.
    pub fn observe(&mut self, kind: &str, data: &Value) -> Option<String> {
        if kind != "session" {
            return None;
        }
        let id = data.get("id")?.as_str()?.to_string();
        let state = data.get("state")?.as_str()?.to_string();
        let previous = self.states.insert(id.clone(), state.clone());
        if state != "waiting" || previous.as_deref() == Some("waiting") {
            return None;
        }
        let title = data.get("title").and_then(Value::as_str).unwrap_or(&id);
        Some(title.to_string())
    }
}

/// (Re)starts the stream task with the current settings, aborting the previous one.
pub fn restart(app: &AppHandle) {
    let state = app.state::<AppState>();
    let settings = lock(&state.settings).clone();
    let task = tauri::async_runtime::spawn(run(app.clone(), state.http.clone(), settings));
    if let Some(previous) = lock(&state.stream).replace(task) {
        previous.abort();
    }
}

async fn run(app: AppHandle, http: Client, settings: Settings) {
    let daemon = match Daemon::new(&http, &settings) {
        Ok(daemon) => daemon,
        Err(message) => return set_link(&app, LinkStatus::new("unpaired", message)),
    };
    let mut notifier = Notifier::default();
    let mut delay = MIN_BACKOFF;
    loop {
        set_link(&app, LinkStatus::new("connecting", "Connexion au démon…"));
        match connect_once(&app, &daemon, &mut notifier).await {
            Ok(()) => {
                delay = MIN_BACKOFF;
                set_link(&app, LinkStatus::new("offline", "Flux interrompu, reconnexion…"));
            }
            Err(DaemonError::Unauthorized(message)) => {
                delay = MAX_BACKOFF;
                set_link(&app, LinkStatus::new("unauthorized", message));
            }
            Err(DaemonError::Other(message)) => set_link(&app, LinkStatus::new("offline", message)),
        }
        tokio::time::sleep(delay).await;
        delay = (delay * 2).min(MAX_BACKOFF);
    }
}

async fn connect_once(app: &AppHandle, daemon: &Daemon, notifier: &mut Notifier) -> Result<(), DaemonError> {
    let response = daemon.stream().await?;
    set_link(app, LinkStatus::new("connected", "Connecté au démon."));
    let mut parser = SseParser::default();
    let mut body = std::pin::pin!(response.bytes_stream());
    while let Some(chunk) = body.next().await {
        let bytes = chunk.map_err(|err| DaemonError::Other(format!("Flux interrompu : {err}")))?;
        for event in parser.feed(&bytes) {
            dispatch(app, notifier, event);
        }
    }
    Ok(())
}

fn dispatch(app: &AppHandle, notifier: &mut Notifier, event: SseEvent) {
    let data: Value = match serde_json::from_str(&event.data) {
        Ok(data) => data,
        Err(err) => {
            eprintln!("omnigroup: ignored malformed '{}' event: {err}", event.event);
            return;
        }
    };
    if let Some(title) = notifier.observe(&event.event, &data) {
        notify_waiting(app, &title);
    }
    if let Err(err) = app.emit(STREAM_EVENT, json!({ "type": event.event, "data": data })) {
        eprintln!("omnigroup: stream event not delivered: {err}");
    }
}

fn notify_waiting(app: &AppHandle, title: &str) {
    let shown = app
        .notification()
        .builder()
        .title("Omnigroup — un agent vous attend")
        .body(format!("{title} attend votre réponse."))
        .show();
    if let Err(err) = shown {
        eprintln!("omnigroup: notification failed: {err}");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn notifies_only_on_transition_to_waiting() {
        let mut n = Notifier::default();
        let working = json!({"id": "claude:1", "state": "working", "title": "Refacto"});
        let waiting = json!({"id": "claude:1", "state": "waiting", "title": "Refacto"});
        assert_eq!(n.observe("session", &working), None);
        assert_eq!(n.observe("session", &waiting), Some("Refacto".into()));
        assert_eq!(n.observe("session", &waiting), None);
        assert_eq!(n.observe("session", &working), None);
        assert_eq!(n.observe("session", &waiting), Some("Refacto".into()));
    }

    #[test]
    fn ignores_other_events_and_bad_shapes() {
        let mut n = Notifier::default();
        assert_eq!(n.observe("step", &json!({"id": "x", "state": "waiting"})), None);
        assert_eq!(n.observe("session", &json!({"state": "waiting"})), None);
    }
}
