//! HTTP client for the omnigroup daemon (bearer auth, loopback only, French errors).

use crate::settings::Settings;
use reqwest::{Client, Response, StatusCode, Url};
use serde_json::Value;
use std::time::Duration;

const CONNECT_TIMEOUT: Duration = Duration::from_secs(3);
/// Longer than the daemon's 15 s SSE heartbeat, so a silent stream is detected.
const READ_TIMEOUT: Duration = Duration::from_secs(45);
const REQUEST_TIMEOUT: Duration = Duration::from_secs(20);
const ID_MAX: usize = 200;

#[derive(Debug)]
pub enum DaemonError {
    Unauthorized(String),
    Other(String),
}

impl DaemonError {
    pub fn message(&self) -> String {
        match self {
            DaemonError::Unauthorized(m) | DaemonError::Other(m) => m.clone(),
        }
    }
}

pub fn http_client() -> Result<Client, String> {
    Client::builder()
        .connect_timeout(CONNECT_TIMEOUT)
        .read_timeout(READ_TIMEOUT)
        .no_proxy()
        .build()
        .map_err(|err| format!("Client HTTP impossible à créer : {err}"))
}

/// A session or pending id from the webview: non-empty, bounded, no control characters.
pub fn validate_id(id: &str) -> Result<&str, String> {
    if id.is_empty() || id.len() > ID_MAX || id.chars().any(char::is_control) {
        return Err("Identifiant invalide.".into());
    }
    Ok(id)
}

pub struct Daemon {
    http: Client,
    base: Url,
    token: String,
}

impl Daemon {
    pub fn new(http: &Client, settings: &Settings) -> Result<Self, String> {
        if settings.token.is_empty() {
            return Err("Widget non couplé : ouvrez les réglages puis « Coupler via WSL ».".into());
        }
        let base = Url::parse(&settings.daemon_url)
            .map_err(|_| "Adresse du démon invalide.".to_string())?;
        Ok(Self { http: http.clone(), base, token: settings.token.clone() })
    }

    fn url(&self, segments: &[&str]) -> Result<Url, DaemonError> {
        let mut url = self.base.clone();
        url.path_segments_mut()
            .map_err(|_| DaemonError::Other("Adresse du démon invalide.".into()))?
            .clear()
            .extend(segments);
        Ok(url)
    }

    fn describe(&self, err: reqwest::Error) -> DaemonError {
        let message = if err.is_connect() {
            format!(
                "Démon injoignable sur {}. Lancez « omniscient group serve » dans WSL.",
                self.base.as_str().trim_end_matches('/')
            )
        } else if err.is_timeout() {
            "Le démon ne répond pas (délai dépassé).".to_string()
        } else {
            format!("Erreur réseau : {err}")
        };
        DaemonError::Other(message)
    }

    async fn check(&self, resp: Response) -> Result<Response, DaemonError> {
        let status = resp.status();
        if status.is_success() {
            return Ok(resp);
        }
        if status == StatusCode::UNAUTHORIZED || status == StatusCode::FORBIDDEN {
            return Err(DaemonError::Unauthorized(
                "Jeton refusé par le démon : recouplez le widget dans les réglages.".into(),
            ));
        }
        if status == StatusCode::NOT_FOUND {
            return Err(DaemonError::Other("Introuvable (session ou demande terminée ?).".into()));
        }
        let detail = resp
            .json::<Value>()
            .await
            .ok()
            .and_then(|v| v.get("error").and_then(Value::as_str).map(str::to_string))
            .unwrap_or_default();
        Err(DaemonError::Other(format!("Le démon a répondu {status}. {detail}").trim().to_string()))
    }

    async fn json(&self, resp: Response) -> Result<Value, DaemonError> {
        let resp = self.check(resp).await?;
        resp.json::<Value>()
            .await
            .map_err(|err| DaemonError::Other(format!("Réponse illisible du démon : {err}")))
    }

    pub async fn get(&self, segments: &[&str]) -> Result<Value, DaemonError> {
        let resp = self
            .http
            .get(self.url(segments)?)
            .bearer_auth(&self.token)
            .timeout(REQUEST_TIMEOUT)
            .send()
            .await
            .map_err(|err| self.describe(err))?;
        self.json(resp).await
    }

    pub async fn post(&self, segments: &[&str], body: &Value) -> Result<Value, DaemonError> {
        let resp = self
            .http
            .post(self.url(segments)?)
            .bearer_auth(&self.token)
            .timeout(REQUEST_TIMEOUT)
            .json(body)
            .send()
            .await
            .map_err(|err| self.describe(err))?;
        self.json(resp).await
    }

    /// Opens GET /stream (no overall timeout: only the per-read timeout applies).
    pub async fn stream(&self) -> Result<Response, DaemonError> {
        let resp = self
            .http
            .get(self.url(&["stream"])?)
            .bearer_auth(&self.token)
            .header(reqwest::header::ACCEPT, "text/event-stream")
            .send()
            .await
            .map_err(|err| self.describe(err))?;
        self.check(resp).await
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn ids_are_validated() {
        assert!(validate_id("claude:1234-abcd").is_ok());
        assert!(validate_id("").is_err());
        assert!(validate_id("a\nb").is_err());
        assert!(validate_id(&"x".repeat(ID_MAX + 1)).is_err());
    }

    #[test]
    fn url_segments_are_encoded() {
        let settings = Settings::default().with_token("abcDEF0123456789_-xyz").unwrap();
        let daemon = Daemon::new(&http_client().unwrap(), &settings).unwrap();
        let url = daemon.url(&["sessions", "a/b?c", "jump"]).unwrap();
        assert_eq!(url.as_str(), "http://127.0.0.1:7341/sessions/a%2Fb%3Fc/jump");
    }

    #[test]
    fn unpaired_settings_are_refused() {
        assert!(Daemon::new(&http_client().unwrap(), &Settings::default()).is_err());
    }
}
