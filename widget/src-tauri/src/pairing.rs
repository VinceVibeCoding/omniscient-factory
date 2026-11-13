//! Pairing: fetch the daemon's bearer token by running the token command inside WSL.
//! On Windows: `wsl.exe [-d <distro>] -e bash -lc "<token command>"` (login shell, so
//! ~/.local/bin is on PATH). Elsewhere (Linux dev): `bash -lc "<token command>"`.
//! The command comes from the owner's own settings, never from the daemon.

use crate::settings::validate_token;
use std::time::Duration;
use tokio::process::Command;

const PAIR_TIMEOUT: Duration = Duration::from_secs(30);
const STDERR_MAX: usize = 300;
#[cfg(windows)]
const CREATE_NO_WINDOW: u32 = 0x0800_0000;

fn command(distro: &str, token_command: &str) -> Command {
    #[cfg(windows)]
    {
        let mut cmd = Command::new("wsl.exe");
        if !distro.is_empty() {
            cmd.args(["-d", distro]);
        }
        cmd.args(["-e", "bash", "-lc", token_command]);
        cmd.creation_flags(CREATE_NO_WINDOW);
        cmd
    }
    #[cfg(not(windows))]
    {
        let _ = distro; // no WSL hop when the widget itself runs on Linux
        let mut cmd = Command::new("bash");
        cmd.args(["-lc", token_command]);
        cmd
    }
}

/// wsl.exe writes its own messages in UTF-16LE: drop the NULs before showing them.
fn clean_stderr(bytes: &[u8]) -> String {
    let text: String = String::from_utf8_lossy(bytes).chars().filter(|c| *c != '\0').collect();
    text.trim().chars().take(STDERR_MAX).collect()
}

/// The token is the last non-empty stdout line.
pub fn token_from_stdout(stdout: &[u8]) -> Result<String, String> {
    let text = String::from_utf8_lossy(stdout);
    let last = text.lines().map(str::trim).filter(|l| !l.is_empty()).last().unwrap_or("");
    let token = validate_token(last)?;
    if token.is_empty() {
        return Err("La commande n'a renvoyé aucun jeton.".into());
    }
    Ok(token)
}

pub async fn fetch_token(distro: &str, token_command: &str) -> Result<String, String> {
    let mut cmd = command(distro, token_command);
    cmd.kill_on_drop(true);
    let output = tokio::time::timeout(PAIR_TIMEOUT, cmd.output())
        .await
        .map_err(|_| "WSL ne répond pas (30 s). Vérifiez que la distribution démarre.".to_string())?
        .map_err(|err| format!("Impossible de lancer WSL : {err}"))?;
    if !output.status.success() {
        let stderr = clean_stderr(&output.stderr);
        return Err(format!("La commande du jeton a échoué ({}). {stderr}", output.status)
            .trim()
            .to_string());
    }
    token_from_stdout(&output.stdout)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn takes_last_non_empty_line() {
        let out = b"warning: something\nabcDEF0123456789_-xyz\n\n";
        assert_eq!(token_from_stdout(out).unwrap(), "abcDEF0123456789_-xyz");
    }

    #[test]
    fn rejects_empty_or_garbage() {
        assert!(token_from_stdout(b"\n").is_err());
        assert!(token_from_stdout(b"command not found: omniscient").is_err());
    }

    #[test]
    fn strips_utf16_nuls() {
        assert_eq!(clean_stderr(b"e\0r\0r\0"), "err");
    }
}
