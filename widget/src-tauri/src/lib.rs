//! Omnigroup widget: Rust side (networking, SSE, tray, notifications, docking).

mod client;
mod commands;
mod pairing;
mod settings;
mod sse;
mod state;
mod stream;
mod tray;
mod window;

use state::AppState;
use tauri::Manager;

fn setup(app: &mut tauri::App) -> Result<(), Box<dyn std::error::Error>> {
    let config_dir = app.path().app_config_dir()?;
    let loaded = settings::load(&config_dir).unwrap_or_else(|err| {
        eprintln!("omnigroup: {err} — default settings used");
        settings::Settings::default()
    });
    let http = client::http_client()?;
    app.manage(AppState::new(http, config_dir, loaded));
    tray::build(app)?;
    let window = app.get_webview_window("main").ok_or("main window missing")?;
    window::dock(&window, window::Mode::Pill)?;
    window.show()?;
    stream::restart(app.handle());
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_positioner::init())
        .setup(setup)
        .invoke_handler(tauri::generate_handler![
            commands::list_sessions,
            commands::get_session,
            commands::get_usage,
            commands::get_theme,
            commands::answer_pending,
            commands::dismiss_session,
            commands::jump_session,
            commands::prompt_session,
            commands::get_settings,
            commands::link_status,
            commands::save_settings,
            commands::pair_via_wsl,
            commands::set_window_mode,
        ])
        .run(tauri::generate_context!())
        .expect("error while running the Omnigroup widget");
}
