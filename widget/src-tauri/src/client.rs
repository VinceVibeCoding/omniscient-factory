//! HTTP client for the omnigroup daemon (loopback only, no proxy, no redirects, French
//! errors). Paired calls carry the widget token, but only once `verify` has checked that
//! the daemon knows it (proof.rs); pairing calls carry no credential at all.

use crate::proof::{new_nonce, proves};
use crate::settings::Settings;
use reqwest::{Client, RequestBuilder, Response, StatusCode, Url};
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
        .redirect(reqwest::redirect::Policy::none())
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
    /// A paired daemon client (the token comes from the Windows vault, never the webview).
    pub fn new(http: &Client, settings: &Settings, token: &str) -> Result<Self, String> {
        if token.is_empty() {
            return Err("Widget non couplé : ouvrez les réglages puis « Demander un code ».".into());
        }
        let mut daemon = Self::anonymous(http, settings)?;
        daemon.token = token.to_string();
        Ok(daemon)
    }

    /// A client without credential, for /health and pairing.
    pub fn anonymous(http: &Client, settings: &Settings) -> Result<Self, String> {
        let base = Url::parse(&settings.daemon_url).map_err(|_| "Adresse du démon invalide.".to_string())?;
        Ok(Self { http: http.clone(), base, token: String::new() })
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

    async fn error_detail(resp: Response) -> String {
        let body: Option<Value> = resp.json::<Value>().await.ok();
        body.and_then(|v| v.get("error").and_then(Value::as_str).map(str::to_string))
            .unwrap_or_default()
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
        let detail = Self::error_detail(resp).await;
        Err(DaemonError::Other(format!("Le démon a répondu {status}. {detail}").trim().to_string()))
    }

    async fn json(&self, resp: Response) -> Result<Value, DaemonError> {
        let resp = self.check(resp).await?;
        resp.json::<Value>()
            .await
            .map_err(|err| DaemonError::Other(format!("Réponse illisible du démon : {err}")))
    }

    async fn send(&self, request: RequestBuilder) -> Result<Value, DaemonError> {
        let resp = request
            .bearer_auth(&self.token)
            .timeout(REQUEST_TIMEOUT)
            .send()
            .await
            .map_err(|err| self.describe(err))?;
        self.json(resp).await
    }

    pub async fn get(&self, segments: &[&str]) -> Result<Value, DaemonError> {
        let url = self.url(segments)?;
        self.send(self.http.get(url)).await
    }

    pub async fn post(&self, segments: &[&str], body: &Value) -> Result<Value, DaemonError> {
        let url = self.url(segments)?;
        self.send(self.http.post(url).json(body)).await
    }

    /// POST without any credential (pairing); the daemon's own French error otherwise.
    pub async fn post_public(&self, segments: &[&str], body: &Value) -> Result<Value, DaemonError> {
        let url = self.url(segments)?;
        let resp = self
            .http
            .post(url)
            .timeout(REQUEST_TIMEOUT)
            .json(body)
            .send()
            .await
            .map_err(|err| self.describe(err))?;
        if !resp.status().is_success() {
            let status = resp.status();
            let detail = Self::error_detail(resp).await;
            let message = if detail.is_empty() { format!("Le démon a répondu {status}.") } else { detail };
            return Err(DaemonError::Other(message));
        }
        resp.json::<Value>()
            .await
            .map_err(|err| DaemonError::Other(format!("Réponse illisible du démon : {err}")))
    }

    /// Checks, without sending the token, that the daemon knows its hash (M5).
    pub async fn verify(&self) -> Result<(), DaemonError> {
        let nonce = new_nonce().map_err(DaemonError::Other)?;
        let mut url = self.url(&["health"])?;
        url.query_pairs_mut().append_pair("nonce", &nonce);
        let resp = self
            .http
            .get(url)
            .timeout(REQUEST_TIMEOUT)
            .send()
            .await
            .map_err(|err| self.describe(err))?;
        let body = self.json(resp).await?;
        let proofs = body.get("proofs").cloned().unwrap_or(Value::Null);
        if proves(&proofs, &self.token, &nonce) {
            return Ok(());
        }
        Err(DaemonError::Unauthorized(
            "Ce démon ne reconnaît pas le widget (couplage révoqué ou autre programme) : recouplez-le.".into(),
        ))
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

    const TOKEN: &str = "abcDEF0123456789_-xyz";

    #[test]
    fn ids_are_validated() {
        assert!(validate_id("claude:1234-abcd").is_ok());
        assert!(validate_id("").is_err());
        assert!(validate_id("a\nb").is_err());
        assert!(validate_id(&"x".repeat(ID_MAX + 1)).is_err());
    }

    #[test]
    fn url_segments_are_encoded() {
        let daemon = Daemon::new(&http_client().unwrap(), &Settings::default(), TOKEN).unwrap();
        let url = daemon.url(&["sessions", "a/b?c", "jump"]).unwrap();
        assert_eq!(url.as_str(), "http://127.0.0.1:7341/sessions/a%2Fb%3Fc/jump");
    }

    #[test]
    fn unpaired_daemon_is_refused_but_anonymous_is_allowed() {
        let http = http_client().unwrap();
        assert!(Daemon::new(&http, &Settings::default(), "").is_err());
        let anonymous = Daemon::anonymous(&http, &Settings::default()).unwrap();
        assert!(anonymous.token.is_empty());
    }
}
