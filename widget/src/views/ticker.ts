// Scrolling per-turn cost tape at the bottom of the OSD: "$0.156 Bash · 116".
// The items are drawn twice and the track slides by half its width, so the loop is
// seamless. The OSD is re-rendered on every daemon update: a negative animation-delay
// computed from a fixed start time keeps the tape moving instead of jumping back.
// With prefers-reduced-motion the tape is static and shows the newest turns.
import { h } from "../dom";
import { cost, quartiles } from "../osd-format";
import type { TickerItem } from "../types";

const PX_PER_SECOND = 40;
const APPROX_ITEM_PX = 120;
const MIN_DURATION_S = 12;
const startedAt = performance.now();

function item(t: TickerItem, q: number): HTMLElement {
  return h(
    "span",
    { class: `ticker__item ticker__item--q${q}` },
    h("span", { class: "ticker__cost" }, cost(t.cost_usd)),
    t.tool ? h("span", { class: "ticker__tool" }, t.tool) : h("span", { class: "ticker__tool" }, "texte"),
    h("span", { class: "ticker__tokens" }, `· ${t.output_tokens}`),
  );
}

export function tickerStrip(items: readonly TickerItem[]): HTMLElement {
  if (items.length === 0) {
    return h("div", { class: "ticker ticker--empty", "aria-hidden": "true" }, h("span", { class: "ticker__idle" }, "aucun tour récent"));
  }
  const q = quartiles(items);
  const newestFirst = [...items].reverse();
  const run = () => newestFirst.map((t) => item(t, q.get(t.msg_id) ?? 0));
  const duration = Math.max(MIN_DURATION_S, (items.length * APPROX_ITEM_PX) / PX_PER_SECOND);
  const elapsed = (performance.now() - startedAt) / 1000;
  const track = h(
    "div",
    { class: "ticker__track" },
    h("div", { class: "ticker__run" }, ...run()),
    h("div", { class: "ticker__run ticker__run--dup", "aria-hidden": "true" }, ...run()),
  );
  // CSSOM writes (not style attributes), allowed by the CSP.
  track.style.animationDuration = `${duration}s`;
  track.style.animationDelay = `-${elapsed % duration}s`;
  const total = items.reduce((sum, t) => sum + (t.cost_usd ?? 0), 0);
  return h(
    "div",
    { class: "ticker", role: "marquee", "aria-label": `${items.length} tours récents, ${cost(total)} au total` },
    track,
  );
}
