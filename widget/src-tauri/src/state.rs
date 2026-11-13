//! Shared app state and the link status pushed to the webview.

use crate::settings::Settings;
use crate::window::Mode;
use reqwest::Client;
use serde::Serialize;
use std::path::PathBuf;
use std::sync::{Mutex, MutexGuard};
use tauri::async_runtime::JoinHandle;
use tauri::{AppHandle, Emitter, Manager};

pub const STATUS_EVENT: &str = "group://status";

#[derive(Clone, Debug, Serialize, PartialEq, Eq)]
pub struct LinkStatus {
    pub state: &'static str, // connecting | connected | unpaired | unauthorized | offline
    pub message: String,
}

impl LinkStatus {
    pub fn new(state: &'static str, message: impl Into<String>) -> Self {
        Self { state, message: message.into() }
    }
}

pub struct AppState {
    pub http: Client,
    pub config_dir: PathBuf,
    pub settings: Mutex<Settings>,
    pub link: Mutex<LinkStatus>,
    pub stream: Mutex<Option<JoinHandle<()>>>,
    pub mode: Mutex<Mode>,
}

impl AppState {
    pub fn new(http: Client, config_dir: PathBuf, settings: Settings) -> Self {
        Self {
            http,
            config_dir,
            settings: Mutex::new(settings),
            link: Mutex::new(LinkStatus::new("connecting", "Connexion au démon…")),
            stream: Mutex::new(None),
            mode: Mutex::new(Mode::Pill),
        }
    }
}

/// Lock that survives a panicked holder (the data is plain settings / status values).
pub fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|poisoned| poisoned.into_inner())
}

pub fn set_link(app: &AppHandle, status: LinkStatus) {
    let state = app.state::<AppState>();
    *lock(&state.link) = status.clone();
    if let Err(err) = app.emit(STATUS_EVENT, status) {
        eprintln!("omnigroup: status event not delivered: {err}");
    }
}
