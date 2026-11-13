//! The widget token in the Windows Credential Manager (stored encrypted with DPAPI for
//! the Windows user) through the keyring crate. It is never written to settings.json and
//! never returned to the webview. Off Windows, keyring uses its in-memory mock store.

const SERVICE: &str = "dev.omniscient.omnigroup";
const ACCOUNT: &str = "widget-token";

fn entry() -> Result<keyring::Entry, String> {
    keyring::Entry::new(SERVICE, ACCOUNT).map_err(|err| format!("Coffre Windows inaccessible : {err}"))
}

/// The stored token, None when the widget was never paired (or was unpaired).
pub fn load() -> Result<Option<String>, String> {
    match entry()?.get_password() {
        Ok(token) => Ok(Some(token)),
        Err(keyring::Error::NoEntry) => Ok(None),
        Err(err) => Err(format!("Jeton illisible dans le coffre Windows : {err}")),
    }
}

pub fn save(token: &str) -> Result<(), String> {
    entry()?
        .set_password(token)
        .map_err(|err| format!("Jeton non enregistré dans le coffre Windows : {err}"))
}

/// Forget the token (nothing stored is fine).
pub fn clear() -> Result<(), String> {
    match entry()?.delete_credential() {
        Ok(()) | Err(keyring::Error::NoEntry) => Ok(()),
        Err(err) => Err(format!("Jeton non supprimé du coffre Windows : {err}")),
    }
}
