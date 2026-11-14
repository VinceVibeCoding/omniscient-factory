// OSD formatting: reset timers, tok/min rate, per-turn cost and cost quartiles.
// Inspired by bozdemir/claude-usage-widget (MIT): "3h 51m" under a day, weekday + time
// beyond; the ticker colours each turn by its cost quartile within the visible window.
import type { TickerItem } from "./types";

const FR = "fr-FR";
const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;

/** "42m", "3h 51m" (< 1 day), "ven. 20:00" beyond; "" when unknown or past. */
export function resetIn(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "";
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "";
  const left = at - now;
  if (left <= 0) return "maintenant";
  if (left < HOUR_MS) return `${Math.max(1, Math.round(left / MINUTE_MS))}m`;
  if (left < DAY_MS) {
    const hours = Math.floor(left / HOUR_MS);
    const minutes = Math.floor((left % HOUR_MS) / MINUTE_MS);
    return `${hours}h ${String(minutes).padStart(2, "0")}m`;
  }
  return new Intl.DateTimeFormat(FR, { weekday: "short", hour: "2-digit", minute: "2-digit" }).format(at);
}

/** "980", "6.2k", "12k" tokens per minute. */
export function rate(tpm: number): string {
  if (!Number.isFinite(tpm) || tpm <= 0) return "0";
  if (tpm < 1000) return String(Math.round(tpm));
  const k = tpm / 1000;
  return `${k < 10 ? k.toFixed(1) : Math.round(k)}k`;
}

/** "$0.156" — the ticker keeps the tape's dollar notation, three decimals under $1. */
export function cost(usd: number | null): string {
  if (usd === null || !Number.isFinite(usd)) return "$—";
  return usd < 1 ? `$${usd.toFixed(3)}` : `$${usd.toFixed(2)}`;
}

export function pct(value: number): string {
  return `${Math.round(value)}%`;
}

export type Quartile = 1 | 2 | 3 | 4;

/** Rank-based quartile of each item's cost within `items` (null cost → 0). */
export function quartiles(items: readonly TickerItem[]): Map<string, Quartile | 0> {
  const out = new Map<string, Quartile | 0>();
  const priced = items.filter((i) => i.cost_usd !== null).sort((a, b) => (a.cost_usd ?? 0) - (b.cost_usd ?? 0));
  priced.forEach((item, index) => {
    const q = Math.min(4, Math.floor((index / Math.max(1, priced.length)) * 4) + 1) as Quartile;
    out.set(item.msg_id, q);
  });
  for (const item of items) if (!out.has(item.msg_id)) out.set(item.msg_id, 0);
  return out;
}

/** Usage level for colouring a limit: < 60 ok, 60–85 warn, > 85 high. */
export function limitLevel(value: number): "ok" | "warn" | "high" {
  if (value > 85) return "high";
  if (value >= 60) return "warn";
  return "ok";
}
