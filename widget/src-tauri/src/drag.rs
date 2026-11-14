//! Moving the widget: the webview asks Rust to start an OS drag (no window permission is
//! granted to the webview), every Moved event that `window::dock` did not cause becomes
//! the new anchor, and the anchor is saved once the window has stopped moving.

use crate::position;
use crate::state::{lock, AppState};
use crate::window::dock;
use std::time::Duration;
use tauri::{AppHandle, Manager, PhysicalPosition, Runtime, WebviewWindow};

const SAVE_DELAY: Duration = Duration::from_millis(600);
/// Windows parks minimised windows around (-32000, -32000): never an anchor.
const OFFSCREEN: i32 = -30000;

#[tauri::command]
pub fn start_drag(window: WebviewWindow) -> Result<(), String> {
    window.start_dragging().map_err(|err| format!("Déplacement impossible : {err}"))
}

/// Window moved: ignore our own placements, otherwise remember the new corner.
pub fn on_moved<R: Runtime>(window: &WebviewWindow<R>, pos: PhysicalPosition<i32>) {
    if pos.x <= OFFSCREEN || pos.y <= OFFSCREEN {
        return;
    }
    let Some(state) = window.try_state::<AppState>() else { return };
    {
        let mut placed = lock(&state.placed);
        if *placed == Some((pos.x, pos.y)) {
            *placed = None;
            return;
        }
    }
    let Ok(size) = window.outer_size() else { return };
    let anchor = (pos.x + size.width as i32, pos.y + size.height as i32);
    *lock(&state.anchor) = Some(anchor);
    let seq = {
        let mut seq = lock(&state.move_seq);
        *seq += 1;
        *seq
    };
    let app = window.app_handle().clone();
    tauri::async_runtime::spawn(async move {
        tokio::time::sleep(SAVE_DELAY).await;
        save_if_latest(&app, seq);
    });
}

fn save_if_latest<R: Runtime>(app: &AppHandle<R>, seq: u64) {
    let state = app.state::<AppState>();
    let latest = *lock(&state.move_seq);
    if latest != seq {
        return; // still moving: a later event will save
    }
    let anchor = *lock(&state.anchor);
    if let Err(err) = position::save(&state.config_dir, anchor) {
        eprintln!("omnigroup: {err}");
    }
}

/// Tray « Replacer dans le coin »: forget the anchor and dock in the corner again.
pub fn reset<R: Runtime>(app: &AppHandle<R>) {
    let state = app.state::<AppState>();
    *lock(&state.anchor) = None;
    if let Err(err) = position::save(&state.config_dir, None) {
        eprintln!("omnigroup: {err}");
    }
    let Some(window) = app.get_webview_window("main") else { return };
    let mode = *lock(&state.mode);
    if let Err(err) = dock(&window, mode) {
        eprintln!("omnigroup: {err}");
    }
}
