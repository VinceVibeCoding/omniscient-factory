// Two layers of colour:
// - the "default" skin uses the home's palette (GET /theme → CSS variables on :root),
//   falling back to the omniscient palette defined in styles.css;
// - the "terminal" and "dashboard" skins (html[data-theme]) bring their own palettes in
//   themes.css, so the home palette's inline variables are removed while they are active.
import type { Skin } from "./types";

const HEX = /^#[0-9a-fA-F]{6}$/;
const SLOTS = [
  "background",
  "surface",
  "foreground",
  "muted",
  "primary",
  "secondary",
  "accent",
  "success",
  "warning",
  "error",
] as const;

let homePalette = new Map<string, string>();
let skin: Skin = "default";

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

/** Accepts {colors:{slot:"#hex"}} or a flat {slot:"#hex"} object; returns valid slots. */
export function parseTheme(raw: unknown): Map<string, string> {
  const out = new Map<string, string>();
  if (!isRecord(raw)) return out;
  const source = isRecord(raw.colors) ? raw.colors : isRecord(raw.palette) ? raw.palette : raw;
  for (const slot of SLOTS) {
    const value = source[slot];
    if (typeof value === "string" && HEX.test(value)) out.set(slot, value.toLowerCase());
  }
  return out;
}

function paint(): void {
  const root = document.documentElement;
  for (const slot of SLOTS) root.style.removeProperty(`--${slot}`);
  if (skin !== "default") return;
  for (const [slot, value] of homePalette) root.style.setProperty(`--${slot}`, value);
}

/** The home palette (GET /theme); only visible with the "default" skin. */
export function applyTheme(raw: unknown): number {
  homePalette = parseTheme(raw);
  paint();
  return homePalette.size;
}

/** Switches the skin (config `theme`). */
export function applySkin(next: Skin): void {
  skin = next;
  document.documentElement.dataset.theme = next;
  paint();
}

export function currentSkin(): Skin {
  return skin;
}
