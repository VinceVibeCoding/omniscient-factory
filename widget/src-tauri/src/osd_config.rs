//! Pure helpers for the OSD config (theme, OSD view, ticker): patch validation, the
//! menu snapshot read from the daemon's effective view, and context-menu ids.
//! Effective view shape: {"config": {"theme","osd_view","show_ticker"}, "sources": {key: "default"|"config"|"settings"}}.

use serde_json::{json, Map, Value};

pub const THEMES: [(&str, &str); 3] = [("default", "Default"), ("terminal", "Terminal"), ("dashboard", "Dashboard")];
pub const VIEWS: [(&str, &str); 2] = [("bars", "Bars"), ("gauge", "Gauge")];
pub const MENU_PREFIX: &str = "ctx:";

/// A partial config from the webview: known keys only, allowed values only, not empty.
pub fn validate_patch(patch: &Value) -> Result<Value, String> {
    let obj = patch.as_object().ok_or("Réglage invalide.")?;
    if obj.is_empty() {
        return Err("Aucun réglage à changer.".into());
    }
    let mut clean = Map::new();
    for (key, value) in obj {
        let ok = match key.as_str() {
            "theme" => value.as_str().is_some_and(|v| THEMES.iter().any(|(id, _)| *id == v)),
            "osd_view" => value.as_str().is_some_and(|v| VIEWS.iter().any(|(id, _)| *id == v)),
            "show_ticker" => value.is_boolean(),
            _ => return Err(format!("Réglage inconnu : {key}")),
        };
        if !ok {
            return Err(format!("Valeur invalide pour {key}."));
        }
        clean.insert(key.clone(), value.clone());
    }
    Ok(Value::Object(clean))
}

/// What the context menu needs from the effective view (defaults when unknown).
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct MenuSnapshot {
    pub theme: String,
    pub osd_view: String,
    pub show_ticker: bool,
    pub theme_locked: bool,
    pub view_locked: bool,
    pub ticker_locked: bool,
}

impl MenuSnapshot {
    pub fn from_view(view: Option<&Value>) -> Self {
        let config = view.and_then(|v| v.get("config"));
        let sources = view.and_then(|v| v.get("sources"));
        let text = |key: &str, fallback: &str| {
            config.and_then(|c| c.get(key)).and_then(Value::as_str).unwrap_or(fallback).to_string()
        };
        let locked = |key: &str| sources.and_then(|s| s.get(key)).and_then(Value::as_str) == Some("settings");
        Self {
            theme: text("theme", "default"),
            osd_view: text("osd_view", "bars"),
            show_ticker: config.and_then(|c| c.get("show_ticker")).and_then(Value::as_bool).unwrap_or(true),
            theme_locked: locked("theme"),
            view_locked: locked("osd_view"),
            ticker_locked: locked("show_ticker"),
        }
    }
}

/// A context-menu click, decoded from its id.
#[derive(Clone, Debug, PartialEq)]
pub enum MenuAction {
    Patch(Value),
    Corner,
    Quit,
}

pub fn view_id(view: &str) -> String {
    format!("{MENU_PREFIX}view:{view}")
}

pub fn theme_id(theme: &str) -> String {
    format!("{MENU_PREFIX}theme:{theme}")
}

pub const TICKER_ID: &str = "ctx:ticker";
pub const CORNER_ID: &str = "ctx:corner";
pub const QUIT_ID: &str = "ctx:quit";

/// None for ids that are not ours (tray menu, unknown values).
pub fn parse_menu_id(id: &str, current: &MenuSnapshot) -> Option<MenuAction> {
    match id {
        TICKER_ID => return Some(MenuAction::Patch(json!({ "show_ticker": !current.show_ticker }))),
        CORNER_ID => return Some(MenuAction::Corner),
        QUIT_ID => return Some(MenuAction::Quit),
        _ => {}
    }
    let rest = id.strip_prefix(MENU_PREFIX)?;
    if let Some(view) = rest.strip_prefix("view:") {
        return VIEWS.iter().any(|(v, _)| *v == view).then(|| MenuAction::Patch(json!({ "osd_view": view })));
    }
    let theme = rest.strip_prefix("theme:")?;
    THEMES.iter().any(|(t, _)| *t == theme).then(|| MenuAction::Patch(json!({ "theme": theme })))
}

/// Menu label, with the reason when the key is fixed in ~/.claude/settings.json.
pub fn label(text: &str, locked: bool) -> String {
    if locked {
        format!("{text} (settings.json)")
    } else {
        text.to_string()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn patches_are_validated() {
        let ok = json!({"theme": "terminal", "osd_view": "gauge", "show_ticker": false});
        assert_eq!(validate_patch(&ok).unwrap(), ok);
        assert!(validate_patch(&json!({})).is_err());
        assert!(validate_patch(&json!({"theme": "neon"})).is_err());
        assert!(validate_patch(&json!({"osd_view": 3})).is_err());
        assert!(validate_patch(&json!({"show_ticker": "yes"})).is_err());
        assert!(validate_patch(&json!({"token": "x"})).is_err());
        assert!(validate_patch(&json!("theme")).is_err());
    }

    #[test]
    fn snapshot_reads_view_and_locks() {
        let view = json!({
            "config": {"theme": "dashboard", "osd_view": "gauge", "show_ticker": false},
            "sources": {"theme": "settings", "osd_view": "config", "show_ticker": "default"}
        });
        let snap = MenuSnapshot::from_view(Some(&view));
        assert_eq!(snap.theme, "dashboard");
        assert_eq!(snap.osd_view, "gauge");
        assert!(!snap.show_ticker);
        assert!(snap.theme_locked && !snap.view_locked && !snap.ticker_locked);
        let empty = MenuSnapshot::from_view(None);
        assert_eq!((empty.theme.as_str(), empty.osd_view.as_str(), empty.show_ticker), ("default", "bars", true));
    }

    #[test]
    fn menu_ids_round_trip() {
        let snap = MenuSnapshot::from_view(None);
        assert_eq!(parse_menu_id(&view_id("gauge"), &snap), Some(MenuAction::Patch(json!({"osd_view": "gauge"}))));
        assert_eq!(parse_menu_id(&theme_id("terminal"), &snap), Some(MenuAction::Patch(json!({"theme": "terminal"}))));
        assert_eq!(parse_menu_id(TICKER_ID, &snap), Some(MenuAction::Patch(json!({"show_ticker": false}))));
        assert_eq!(parse_menu_id(CORNER_ID, &snap), Some(MenuAction::Corner));
        assert_eq!(parse_menu_id(QUIT_ID, &snap), Some(MenuAction::Quit));
        assert_eq!(parse_menu_id("ctx:theme:neon", &snap), None);
        assert_eq!(parse_menu_id("toggle", &snap), None);
        assert_eq!(parse_menu_id("quit", &snap), None);
    }

    #[test]
    fn locked_labels_say_why() {
        assert_eq!(label("Terminal", true), "Terminal (settings.json)");
        assert_eq!(label("Terminal", false), "Terminal");
    }
}
