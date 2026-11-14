//! Window modes (pill / panel) and placement: bottom-right of the work area (above the
//! taskbar) until the owner drags the widget, then wherever they left it (position.rs).
//! Falls back to tauri-plugin-positioner when no monitor is known.

use crate::position::{contains, place, Rect};
use crate::state::{lock, AppState};
use tauri::{LogicalSize, Manager, PhysicalPosition, Runtime, WebviewWindow};
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

/// The monitor work area holding `anchor` (else the window's monitor, else the primary).
fn work_area<R: Runtime>(window: &WebviewWindow<R>, anchor: Option<(i32, i32)>) -> Option<(Rect, f64)> {
    let as_rect = |m: &tauri::Monitor| {
        let area = m.work_area();
        let rect = Rect {
            x: area.position.x,
            y: area.position.y,
            width: area.size.width as i32,
            height: area.size.height as i32,
        };
        (rect, m.scale_factor())
    };
    if let Some(point) = anchor {
        let monitors = window.available_monitors().unwrap_or_default();
        if let Some(found) = monitors.iter().map(as_rect).find(|(rect, _)| contains(*rect, point)) {
            return Some(found);
        }
    }
    let monitor = window
        .current_monitor()
        .ok()
        .flatten()
        .or_else(|| window.primary_monitor().ok().flatten())?;
    Some(as_rect(&monitor))
}

/// Places the window so its bottom-right corner sits on the owner's anchor (where they
/// dragged it), or MARGIN logical pixels from the work-area corner, always fully visible.
pub fn dock<R: Runtime>(window: &WebviewWindow<R>, mode: Mode) -> Result<(), String> {
    let state = window.state::<AppState>();
    let anchor = *lock(&state.anchor);
    let Some((area, scale)) = work_area(window, anchor) else {
        return window
            .move_window(Position::BottomRight)
            .map_err(|err| format!("Placement impossible : {err}"));
    };
    let (w, h) = mode.size();
    let size = ((w * scale).round() as i32, (h * scale).round() as i32);
    let margin = (MARGIN * scale).round() as i32;
    let (x, y) = place(area, size, anchor, margin);
    // Remember our own move so the Moved event it triggers is not taken for a drag.
    *lock(&state.placed) = Some((x, y));
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
