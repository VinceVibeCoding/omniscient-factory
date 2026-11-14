// The always-on-top OSD (replaces the pill): header with subagent counter, pending
// alert and live tok/min badge; Session / Weekly utilisation as bars or rings with reset
// timers; the per-turn cost ticker along the bottom. Left click opens the panel; the
// right-click menu (OSD View ▸, Thème ▸ …) is native and handled in main.ts / Rust.
// Layout inspired by bozdemir/claude-usage-widget (MIT).
import { h, svg } from "../dom";
import { eyeMark } from "../eye";
import { limitLevel, pct, rate, resetIn } from "../osd-format";
import type { Limit, LinkStatus, Osd, Session, Skin, WidgetConfig } from "../types";
import { countSessions } from "./pill";
import { tickerStrip } from "./ticker";

export interface OsdModel {
  osd: Osd | null;
  config: WidgetConfig;
  link: LinkStatus;
  sessions: readonly Session[];
}

const ASCII_CELLS = 20;
const RING_R = 30;
const RING_C = 2 * Math.PI * RING_R;
const PLACEHOLDER: readonly Limit[] = [
  { id: "five_hour", label: "Session", used_pct: 0, resets_at: null },
  { id: "seven_day", label: "Weekly", used_pct: 0, resets_at: null },
];

function header(m: OsdModel): HTMLElement {
  const osd = m.osd;
  const waiting = countSessions([...m.sessions]).waiting;
  const subagents = osd?.subagents ?? 0;
  const live = osd?.live.is_live === true;
  return h(
    "div",
    { class: "osd__head" },
    h("span", { class: "osd__title" }, "Claude"),
    subagents > 0
      ? h("span", { class: "osd__agents", title: `${subagents} sous-agents actifs` }, `⚙ ${subagents}`, h("span", { class: "osd__agents-word" }, " subagents"))
      : null,
    waiting > 0 ? h("span", { class: "osd__alert", title: `${waiting} en attente de réponse` }, `◆ ${waiting}`) : null,
    h("span", { class: "osd__spacer" }),
    live
      ? h("span", { class: "osd__live" }, `● LIVE ${rate(osd?.live.tokens_per_minute ?? 0)} tok/min`)
      : h("span", { class: "osd__idle" }, "○ repos"),
  );
}

function asciiBar(value: number): HTMLElement {
  const full = Math.round((value / 100) * ASCII_CELLS);
  return h(
    "span",
    { class: "osd__ascii", "aria-hidden": "true" },
    h("span", { class: "osd__ascii-fill" }, "█".repeat(full)),
    h("span", { class: "osd__ascii-track" }, "░".repeat(ASCII_CELLS - full)),
  );
}

function meterBar(value: number): HTMLElement {
  const fill = h("span", { class: `osd__fill osd__fill--${limitLevel(value)}` });
  fill.style.width = `${value}%`; // CSSOM, allowed by the CSP
  return h("span", { class: "osd__track", "aria-hidden": "true" }, fill);
}

function barRow(limit: Limit, skin: Skin, known: boolean): HTMLElement {
  const value = known ? pct(limit.used_pct) : "–";
  const reset = known ? resetIn(limit.resets_at) : "";
  return h(
    "div",
    {
      class: `osd__row osd__row--${limitLevel(limit.used_pct)}`,
      role: "meter",
      "aria-valuemin": 0,
      "aria-valuemax": 100,
      "aria-valuenow": Math.round(limit.used_pct),
      "aria-label": `${limit.label} ${value}${reset ? `, réinitialisé dans ${reset}` : ""}`,
    },
    h("span", { class: "osd__label" }, limit.label),
    h("span", { class: "osd__reset" }, reset),
    h("span", { class: "osd__pct" }, value),
    skin === "terminal" ? asciiBar(known ? limit.used_pct : 0) : meterBar(known ? limit.used_pct : 0),
  );
}

function ring(limit: Limit, known: boolean): HTMLElement {
  const root = svg("svg", { viewBox: "0 0 80 80", class: "osd__ring", "aria-hidden": "true" });
  const arc = svg("circle", {
    cx: 40,
    cy: 40,
    r: RING_R,
    class: `osd__ring-fill osd__ring-fill--${limitLevel(limit.used_pct)}`,
    "stroke-dasharray": `${(RING_C * (known ? limit.used_pct : 0)) / 100} ${RING_C}`,
    transform: "rotate(-90 40 40)",
  });
  root.append(svg("circle", { cx: 40, cy: 40, r: RING_R, class: "osd__ring-track" }), arc);
  const value = known ? pct(limit.used_pct) : "–";
  const reset = known ? resetIn(limit.resets_at) : "";
  return h(
    "div",
    { class: "osd__gauge", role: "meter", "aria-valuenow": Math.round(limit.used_pct), "aria-label": `${limit.label} ${value}` },
    h("div", { class: "osd__ring-box" }, root, h("span", { class: "osd__ring-pct" }, value)),
    h("span", { class: "osd__label" }, limit.label),
    h("span", { class: "osd__reset" }, reset),
  );
}

function body(m: OsdModel): HTMLElement {
  const known = (m.osd?.limits.length ?? 0) > 0;
  const limits = known ? (m.osd?.limits ?? []).slice(0, 3) : PLACEHOLDER;
  const content =
    m.config.osd_view === "gauge"
      ? h("div", { class: "osd__gauges" }, ...limits.slice(0, 2).map((l) => ring(l, known)))
      : h("div", { class: "osd__rows" }, ...limits.map((l) => barRow(l, m.config.theme, known)));
  if (known) return content;
  return h("div", { class: "osd__body" }, content, h("p", { class: "osd__hint" }, "Limites inconnues : activez la statusline (omniscient group setup)."));
}

function offline(m: OsdModel): HTMLElement {
  return h(
    "div",
    { class: "osd__offline" },
    eyeMark(26, false),
    h("p", { class: "osd__offline-text" }, m.link.message),
    h("p", { class: "osd__hint" }, "Cliquez pour ouvrir les réglages."),
  );
}

export function osdView(m: OsdModel, expand: () => void): HTMLElement {
  const online = m.link.state === "connected";
  const waiting = countSessions([...m.sessions]).waiting > 0;
  const label = online
    ? "Omnigroup : utilisation Claude. Clic : ouvrir le panneau. Clic droit : vue, thème, ticker."
    : `Omnigroup : ${m.link.message} Ouvrir le panneau.`;
  const root = h(
    "div",
    {
      class: `osd osd--${m.config.osd_view} ${waiting ? "osd--alert" : ""} ${online ? "" : "osd--offline"}`,
      role: "button",
      tabindex: 0,
      "aria-label": label,
      title: label,
      onclick: expand,
      onkeydown: ((event: KeyboardEvent) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          expand();
        }
      }) as EventListener,
    },
    header(m),
    online ? body(m) : offline(m),
    online && m.config.show_ticker ? tickerStrip(m.osd?.ticker ?? []) : null,
  );
  return root;
}
