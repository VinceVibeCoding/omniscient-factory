//! Where the owner dragged the widget. Stored as the window's bottom-right corner (the
//! "anchor", physical pixels) in `window.json` next to settings.json, so the pill and the
//! taller panel both grow from the same corner. No anchor = docked in the screen corner.

use serde::{Deserialize, Serialize};
use std::fs;
use std::path::Path;

const FILE_NAME: &str = "window.json";

#[derive(Clone, Copy, Debug, PartialEq, Eq, Serialize, Deserialize)]
pub struct Rect {
    pub x: i32,
    pub y: i32,
    pub width: i32,
    pub height: i32,
}

#[derive(Debug, Default, Serialize, Deserialize)]
struct Stored {
    anchor: Option<[i32; 2]>,
}

/// Top-left position of a `size` window whose bottom-right corner sits on `anchor`
/// (or `margin` pixels inside the work area's corner when there is none), kept fully
/// inside `area`.
pub fn place(area: Rect, size: (i32, i32), anchor: Option<(i32, i32)>, margin: i32) -> (i32, i32) {
    let (width, height) = size;
    let right = area.x + area.width;
    let bottom = area.y + area.height;
    let (ax, ay) = anchor.unwrap_or((right - margin, bottom - margin));
    let x = (ax - width).clamp(area.x, (right - width).max(area.x));
    let y = (ay - height).clamp(area.y, (bottom - height).max(area.y));
    (x, y)
}

/// True when `point` lies inside `area` (used to pick the monitor of an anchor).
pub fn contains(area: Rect, point: (i32, i32)) -> bool {
    let (px, py) = point;
    px >= area.x && px <= area.x + area.width && py >= area.y && py <= area.y + area.height
}

/// The saved anchor; a missing or unreadable file means "docked in the corner".
pub fn load(dir: &Path) -> Option<(i32, i32)> {
    let text = fs::read_to_string(dir.join(FILE_NAME)).ok()?;
    match serde_json::from_str::<Stored>(&text) {
        Ok(stored) => stored.anchor.map(|[x, y]| (x, y)),
        Err(err) => {
            eprintln!("omnigroup: window.json ignored: {err}");
            None
        }
    }
}

/// Atomic write (temp file + rename); `None` forgets the position.
pub fn save(dir: &Path, anchor: Option<(i32, i32)>) -> Result<(), String> {
    fs::create_dir_all(dir).map_err(|err| format!("Dossier des réglages inaccessible : {err}"))?;
    let stored = Stored { anchor: anchor.map(|(x, y)| [x, y]) };
    let text = serde_json::to_string(&stored).map_err(|err| format!("Position non sérialisable : {err}"))?;
    let tmp = dir.join(format!(".{FILE_NAME}.tmp"));
    fs::write(&tmp, text).map_err(|err| format!("Écriture de la position impossible : {err}"))?;
    fs::rename(&tmp, dir.join(FILE_NAME)).map_err(|err| format!("Écriture de la position impossible : {err}"))
}

#[cfg(test)]
mod tests {
    use super::*;

    const AREA: Rect = Rect { x: 0, y: 0, width: 1920, height: 1032 };

    #[test]
    fn docks_in_the_corner_without_anchor() {
        assert_eq!(place(AREA, (236, 60), None, 12), (1672, 960));
    }

    #[test]
    fn bottom_right_follows_the_anchor() {
        assert_eq!(place(AREA, (236, 60), Some((800, 500)), 12), (564, 440));
        assert_eq!(place(AREA, (360, 520), Some((800, 500)), 12), (440, 0));
    }

    #[test]
    fn stays_inside_the_work_area() {
        assert_eq!(place(AREA, (236, 60), Some((100, 30)), 12), (0, 0));
        assert_eq!(place(AREA, (236, 60), Some((5000, 5000)), 12), (1684, 972));
        let second = Rect { x: 1920, y: 0, width: 1280, height: 1024 };
        assert_eq!(place(second, (236, 60), Some((1950, 40)), 12), (1920, 0));
    }

    #[test]
    fn contains_checks_bounds() {
        assert!(contains(AREA, (10, 10)));
        assert!(!contains(AREA, (2000, 10)));
    }

    #[test]
    fn save_load_and_forget() {
        let dir = std::env::temp_dir().join(format!("omnigroup-pos-{}", std::process::id()));
        assert_eq!(load(&dir), None);
        save(&dir, Some((900, 700))).unwrap();
        assert_eq!(load(&dir), Some((900, 700)));
        save(&dir, None).unwrap();
        assert_eq!(load(&dir), None);
        fs::write(dir.join(FILE_NAME), "not json").unwrap();
        assert_eq!(load(&dir), None);
        fs::remove_dir_all(&dir).unwrap();
    }
}
