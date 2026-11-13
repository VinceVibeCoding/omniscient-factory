//! Window modes (pill / panel) and docking in the bottom-right corner of the work area
//! (above the taskbar). Falls back to tauri-plugin-positioner when the monitor is unknown.

use tauri::{LogicalSize, PhysicalPosition, Runtime, WebviewWindow};
use tauri_plugin_positioner::{Position, WindowExt};

const PILL: (f64, f64) = (236.0, 60.0);
const PANEL: (f64, f64) = (360.0, 520.0);
const MARGIN: f64 = 12.0;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Mode {
    Pill,
    Panel,
}

impl Mode {
    pub fn parse(raw: &str) -> Result<Mode, String> {
        match raw {
            "pill" => Ok(Mode::Pill),
            "panel" => Ok(Mode::Panel),
            _ => Err(format!("Mode de fenêtre inconnu : {raw}")),
        }
    }

    fn size(self) -> (f64, f64) {
        match self {
            Mode::Pill => PILL,
            Mode::Panel => PANEL,
        }
    }
}

pub fn set_mode<R: Runtime>(window: &WebviewWindow<R>, mode: Mode) -> Result<(), String> {
    let (w, h) = mode.size();
    window
        .set_size(LogicalSize::new(w, h))
        .map_err(|err| format!("Redimensionnement impossible : {err}"))?;
    dock(window, mode)
}

/// Bottom-right of the current monitor's work area, MARGIN logical pixels from the edges.
pub fn dock<R: Runtime>(window: &WebviewWindow<R>, mode: Mode) -> Result<(), String> {
    let monitor = window
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| window.primary_monitor().ok().flatten());
    let Some(monitor) = monitor else {
        return window
            .move_window(Position::BottomRight)
            .map_err(|err| format!("Placement impossible : {err}"));
    };
    let scale = monitor.scale_factor();
    let area = monitor.work_area();
    let (w, h) = mode.size();
    let width = (w * scale).round() as i32;
    let height = (h * scale).round() as i32;
    let margin = (MARGIN * scale).round() as i32;
    let x = area.position.x + area.size.width as i32 - width - margin;
    let y = area.position.y + area.size.height as i32 - height - margin;
    window
        .set_position(PhysicalPosition::new(x, y))
        .map_err(|err| format!("Placement impossible : {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_modes() {
        assert_eq!(Mode::parse("pill").unwrap(), Mode::Pill);
        assert_eq!(Mode::parse("panel").unwrap(), Mode::Panel);
        assert!(Mode::parse("huge").is_err());
    }
}
