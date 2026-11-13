//! Widget settings: daemon URL, bearer token, WSL distro and the token command.
//! Stored as JSON in the app config dir (per Windows user, %APPDATA%).

use reqwest::Url;
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

pub const DEFAULT_URL: &str = "http://127.0.0.1:7341";
pub const DEFAULT_TOKEN_COMMAND: &str = "omniscient group token";
const FILE_NAME: &str = "settings.json";
const LOOPBACK_HOSTS: [&str; 3] = ["127.0.0.1", "localhost", "[::1]"];
const TOKEN_MIN: usize = 16;
const TOKEN_MAX: usize = 256;
const DISTRO_MAX: usize = 64;
const COMMAND_MAX: usize = 200;

#[derive(Clone, Debug, PartialEq, Eq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub daemon_url: String,
    pub token: String,
    pub wsl_distro: String,
    pub token_command: String,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            daemon_url: DEFAULT_URL.to_string(),
            token: String::new(),
            wsl_distro: String::new(),
            token_command: DEFAULT_TOKEN_COMMAND.to_string(),
        }
    }
}

impl Settings {
    /// A normalised copy, or a French error naming the bad field.
    pub fn validated(&self) -> Result<Settings, String> {
        Ok(Settings {
            daemon_url: validate_url(&self.daemon_url)?,
            token: validate_token(&self.token)?,
            wsl_distro: validate_distro(&self.wsl_distro)?,
            token_command: validate_command(&self.token_command)?,
        })
    }

    pub fn with_token(&self, token: &str) -> Result<Settings, String> {
        Settings {
            token: token.to_string(),
            ..self.clone()
        }
        .validated()
    }
}

/// Only plain HTTP on the loopback interface: the token must never leave the machine.
pub fn validate_url(raw: &str) -> Result<String, String> {
    let raw = raw.trim();
    let url = Url::parse(raw).map_err(|_| format!("Adresse du démon invalide : « {raw} »."))?;
    let host = url.host_str().unwrap_or_default();
    if url.scheme() != "http" || !LOOPBACK_HOSTS.contains(&host) {
        return Err("L'adresse du démon doit être http://127.0.0.1:<port> ou http://localhost:<port>.".into());
    }
    if !url.username().is_empty() || url.password().is_some() || url.query().is_some() {
        return Err("L'adresse du démon ne doit contenir ni identifiants ni paramètres.".into());
    }
    if url.path() != "/" {
        return Err("L'adresse du démon ne doit pas contenir de chemin.".into());
    }
    Ok(raw.trim_end_matches('/').to_string())
}

/// Empty (not paired yet) or a URL-safe token (secrets.token_urlsafe).
pub fn validate_token(raw: &str) -> Result<String, String> {
    let token = raw.trim();
    if token.is_empty() {
        return Ok(String::new());
    }
    let ok_chars = token
        .chars()
        .all(|c| c.is_ascii_alphanumeric() || c == '-' || c == '_');
    if !ok_chars || !(TOKEN_MIN..=TOKEN_MAX).contains(&token.len()) {
        return Err("Jeton invalide : collez la sortie de « omniscient group token ».".into());
    }
    Ok(token.to_string())
}

pub fn validate_distro(raw: &str) -> Result<String, String> {
    let distro = raw.trim();
    let ok = distro.len() <= DISTRO_MAX
        && distro
            .chars()
            .all(|c| c.is_ascii_alphanumeric() || matches!(c, '-' | '_' | '.'));
    if !ok {
        return Err("Nom de distribution WSL invalide.".into());
    }
    Ok(distro.to_string())
}

pub fn validate_command(raw: &str) -> Result<String, String> {
    let command = raw.trim();
    if command.is_empty() || command.len() > COMMAND_MAX || command.chars().any(char::is_control) {
        return Err("Commande du jeton invalide (une seule ligne, 200 caractères max).".into());
    }
    Ok(command.to_string())
}

/// Missing file → defaults; unreadable or invalid file → French error.
pub fn load(dir: &Path) -> Result<Settings, String> {
    let path = dir.join(FILE_NAME);
    let text = match fs::read_to_string(&path) {
        Ok(text) => text,
        Err(err) if err.kind() == std::io::ErrorKind::NotFound => return Ok(Settings::default()),
        Err(err) => return Err(format!("Réglages illisibles : {err}")),
    };
    let parsed: Settings =
        serde_json::from_str(&text).map_err(|err| format!("Réglages corrompus : {err}"))?;
    parsed.validated()
}

/// Atomic write (temp file + rename) of validated settings.
pub fn save(dir: &Path, settings: &Settings) -> Result<(), String> {
    fs::create_dir_all(dir).map_err(|err| format!("Dossier des réglages inaccessible : {err}"))?;
    let text = serde_json::to_string_pretty(settings)
        .map_err(|err| format!("Réglages non sérialisables : {err}"))?;
    let tmp = dir.join(format!(".{FILE_NAME}.tmp"));
    fs::write(&tmp, text).map_err(|err| format!("Écriture des réglages impossible : {err}"))?;
    fs::rename(&tmp, dir.join(FILE_NAME))
        .map_err(|err| format!("Écriture des réglages impossible : {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn accepts_loopback_urls_and_strips_trailing_slash() {
        assert_eq!(validate_url("http://127.0.0.1:7341/").unwrap(), "http://127.0.0.1:7341");
        assert!(validate_url("http://localhost:7341").is_ok());
        assert!(validate_url("http://[::1]:7341").is_ok());
    }

    #[test]
    fn rejects_remote_or_tls_or_paths() {
        assert!(validate_url("http://example.com:7341").is_err());
        assert!(validate_url("https://127.0.0.1:7341").is_err());
        assert!(validate_url("http://127.0.0.1:7341/api").is_err());
        assert!(validate_url("http://user:pw@127.0.0.1:7341").is_err());
    }

    #[test]
    fn token_rules() {
        assert_eq!(validate_token("  ").unwrap(), "");
        assert!(validate_token("abcDEF0123456789_-xyz").is_ok());
        assert!(validate_token("short").is_err());
        assert!(validate_token("has space in the middle!!").is_err());
    }

    #[test]
    fn distro_and_command_rules() {
        assert!(validate_distro("Ubuntu-24.04").is_ok());
        assert!(validate_distro("bad;name").is_err());
        assert!(validate_command("omniscient group token").is_ok());
        assert!(validate_command("a\nb").is_err());
        assert!(validate_command("").is_err());
    }

    #[test]
    fn save_then_load_round_trips() {
        let dir = std::env::temp_dir().join(format!("omnigroup-test-{}", std::process::id()));
        let settings = Settings::default().with_token("abcDEF0123456789_-xyz").unwrap();
        save(&dir, &settings).unwrap();
        assert_eq!(load(&dir).unwrap(), settings);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn missing_file_gives_defaults() {
        let dir = std::env::temp_dir().join("omnigroup-test-missing-dir");
        assert_eq!(load(&dir).unwrap(), Settings::default());
    }
}
