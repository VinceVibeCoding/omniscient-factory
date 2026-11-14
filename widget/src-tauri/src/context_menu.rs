//! Right-click menu of the OSD (native, built in Rust): « OSD View ▸ Bars / Gauge »,
//! « Thème ▸ Default / Terminal / Dashboard », « Afficher le ticker », « Replacer dans le
//! coin », « Quitter ». Keys fixed in ~/.claude/settings.json are shown disabled.
//! A selection is sent to the daemon (PUT /config, which persists it) and the new
//! effective view is pushed to the webview as a `config` group://event.

use crate::commands::daemon_for;
use crate::osd_config::{
    label, parse_menu_id, theme_id, validate_patch, view_id, MenuAction, MenuSnapshot, CORNER_ID, MENU_PREFIX,
    QUIT_ID, THEMES, TICKER_ID, VIEWS,
};
use crate::state::{lock, AppState};
use crate::stream::STREAM_EVENT;
use serde_json::{json, Value};
use tauri::menu::{CheckMenuItem, IsMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::{AppHandle, Emitter, Manager, WebviewWindow, Wry};

/// PUT /config with a validated patch; caches and returns the new effective view.
pub async fn put_config(state: &AppState, patch: &Value) -> Result<Value, String> {
    let clean = validate_patch(patch)?;
    let daemon = daemon_for(state)?;
    let view = daemon.put(&["config"], &clean).await.map_err(|err| err.message())?;
    *lock(&state.config) = Some(view.clone());
    Ok(view)
}

fn checks(
    app: &AppHandle,
    entries: &[(&str, &str)],
    id: fn(&str) -> String,
    current: &str,
    locked: bool,
) -> tauri::Result<Vec<CheckMenuItem<Wry>>> {
    entries
        .iter()
        .map(|(value, text)| {
            CheckMenuItem::with_id(app, id(value), label(text, locked), !locked, current == *value, None::<&str>)
        })
        .collect()
}

fn submenu(app: &AppHandle, title: &str, items: &[CheckMenuItem<Wry>]) -> tauri::Result<Submenu<Wry>> {
    let refs: Vec<&dyn IsMenuItem<Wry>> = items.iter().map(|item| item as &dyn IsMenuItem<Wry>).collect();
    Submenu::with_items(app, title, true, &refs)
}

fn build(app: &AppHandle, snap: &MenuSnapshot) -> tauri::Result<Menu<Wry>> {
    let views = checks(app, &VIEWS, view_id, &snap.osd_view, snap.view_locked)?;
    let themes = checks(app, &THEMES, theme_id, &snap.theme, snap.theme_locked)?;
    let view_menu = submenu(app, "OSD View", &views)?;
    let theme_menu = submenu(app, "Thème", &themes)?;
    let ticker = CheckMenuItem::with_id(
        app,
        TICKER_ID,
        label("Afficher le ticker", snap.ticker_locked),
        !snap.ticker_locked,
        snap.show_ticker,
        None::<&str>,
    )?;
    let separator = PredefinedMenuItem::separator(app)?;
    let corner = MenuItem::with_id(app, CORNER_ID, "Replacer dans le coin", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, QUIT_ID, "Quitter", true, None::<&str>)?;
    Menu::with_items(app, &[&view_menu, &theme_menu, &ticker, &separator, &corner, &quit])
}

fn snapshot(app: &AppHandle) -> MenuSnapshot {
    let cached = lock(&app.state::<AppState>().config).clone();
    MenuSnapshot::from_view(cached.as_ref())
}

/// Refreshes the cached view when possible (the menu still opens with defaults if not).
async fn refresh(app: &AppHandle) {
    let state = app.state::<AppState>();
    let Ok(daemon) = daemon_for(&state) else { return };
    match daemon.get(&["config"]).await {
        Ok(view) => *lock(&state.config) = Some(view),
        Err(err) => eprintln!("omnigroup: config not refreshed: {}", err.message()),
    }
}

/// Shows the menu at the cursor (built and shown on the main thread).
pub async fn popup(app: AppHandle, window: WebviewWindow) -> Result<(), String> {
    refresh(&app).await;
    let snap = snapshot(&app);
    let handle = app.clone();
    app.run_on_main_thread(move || match build(&handle, &snap) {
        Ok(menu) => {
            if let Err(err) = window.popup_menu(&menu) {
                eprintln!("omnigroup: context menu not shown: {err}");
            }
        }
        Err(err) => eprintln!("omnigroup: context menu not built: {err}"),
    })
    .map_err(|err| format!("Menu impossible à afficher : {err}"))
}

fn emit(app: &AppHandle, kind: &str, data: Value) {
    if let Err(err) = app.emit(STREAM_EVENT, json!({ "type": kind, "data": data })) {
        eprintln!("omnigroup: {kind} event not delivered: {err}");
    }
}

async fn apply(app: AppHandle, patch: Value) {
    let state = app.state::<AppState>();
    match put_config(&state, &patch).await {
        Ok(view) => emit(&app, "config", view),
        Err(message) => {
            eprintln!("omnigroup: config not saved: {message}");
            emit(&app, "menu_error", json!({ "message": message }));
        }
    }
}

fn on_menu(app: &AppHandle, id: &str) {
    if !id.starts_with(MENU_PREFIX) {
        return; // tray menu items are handled by tray.rs
    }
    match parse_menu_id(id, &snapshot(app)) {
        Some(MenuAction::Patch(patch)) => {
            tauri::async_runtime::spawn(apply(app.clone(), patch));
        }
        Some(MenuAction::Corner) => crate::drag::reset(app),
        Some(MenuAction::Quit) => app.exit(0),
        None => eprintln!("omnigroup: unknown menu id {id}"),
    }
}

/// Registers the global menu listener once (setup).
pub fn install(app: &AppHandle) {
    app.on_menu_event(|app, event| on_menu(app, event.id().as_ref()));
}
