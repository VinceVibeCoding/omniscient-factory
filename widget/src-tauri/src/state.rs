//! Shared app state and the link status pushed to the webview.
//!
//! Rust 2021 keeps temporaries of a block's tail expression alive until after the block's
//! locals are dropped: never end a function with `lock(&state.x)…` when `state` is a
//! local (E0597). Bind the value with `let` first.

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
    /// The widget token (from the Windows vault). Never sent to the webview.
    pub token: Mutex<String>,
    /// Bumped by every stream restart (new settings or token).
    pub generation: Mutex<u64>,
    /// The generation whose daemon proved it knows the token (M5); 0 = none.
    pub verified: Mutex<u64>,
    pub link: Mutex<LinkStatus>,
    pub stream: Mutex<Option<JoinHandle<()>>>,
    pub mode: Mutex<Mode>,
}

impl AppState {
    pub fn new(http: Client, config_dir: PathBuf, settings: Settings, token: String) -> Self {
        Self {
            http,
            config_dir,
            settings: Mutex::new(settings),
            token: Mutex::new(token),
            generation: Mutex::new(0),
            verified: Mutex::new(0),
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

/// A new generation (stream restart): nothing is verified any more. Returns it.
pub fn next_generation(app: &AppHandle) -> u64 {
    let state = app.state::<AppState>();
    let mut generation = lock(&state.generation);
    *generation += 1;
    let current = *generation;
    drop(generation);
    *lock(&state.verified) = 0;
    current
}

/// Marks `generation` verified, unless a restart made it stale meanwhile.
pub fn mark_verified(app: &AppHandle, generation: u64) {
    let state = app.state::<AppState>();
    let current = *lock(&state.generation);
    if current == generation {
        *lock(&state.verified) = generation;
    }
}

/// The daemon of `generation` stopped being trusted (stream ended).
pub fn clear_verified(app: &AppHandle, generation: u64) {
    let state = app.state::<AppState>();
    let mut verified = lock(&state.verified);
    if *verified == generation {
        *verified = 0;
    }
}

/// True when the current generation's daemon is verified.
pub fn is_verified(state: &AppState) -> bool {
    let current = *lock(&state.generation);
    let verified = *lock(&state.verified);
    current != 0 && verified == current
}
