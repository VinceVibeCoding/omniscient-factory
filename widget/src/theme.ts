// GET /theme → CSS variables. Unknown or invalid colours keep the omniscient fallback
// defined in styles.css.

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

export function applyTheme(raw: unknown): number {
  const colors = parseTheme(raw);
  const root = document.documentElement;
  for (const [slot, value] of colors) root.style.setProperty(`--${slot}`, value);
  return colors.size;
}
