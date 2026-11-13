// Expanded panel: header + session list (arrow keys move between rows).
import { h } from "../dom";
import { eyeMark } from "../eye";
import { age, clip, cwdTail, sourceLabel, sourceMark, stateLabel } from "../format";
import type { LinkStatus, Session } from "../types";
import { rowGauge } from "./usage";

export interface ListHandlers {
  open(id: string): void;
  collapse(): void;
  settings(): void;
}

export function header(link: LinkStatus, on: ListHandlers, title = "Omnigroup"): HTMLElement {
  return h(
    "header",
    { class: "bar" },
    eyeMark(20, link.state === "connected"),
    h("h1", { class: "bar__title" }, title),
    h("span", {
      class: `link link--${link.state}`,
      role: "img",
      "aria-label": link.message,
      title: link.message,
    }),
    h("button", { class: "icon-btn", "aria-label": "Réglages", title: "Réglages", onclick: on.settings }, "⚙"),
    h("button", { class: "icon-btn", "aria-label": "Réduire", title: "Réduire (Échap)", onclick: on.collapse }, "▁"),
  );
}

function row(s: Session, open: (id: string) => void): HTMLElement {
  const waiting = s.state === "waiting" || s.pending.length > 0;
  const state = waiting ? "waiting" : s.state;
  const tail = cwdTail(s.cwd);
  return h(
    "li",
    {},
    h(
      "button",
      {
        class: `row ${waiting ? "row--waiting" : ""}`,
        "data-row": s.id,
        "aria-label": `${sourceLabel(s.source)} — ${s.title} — ${stateLabel(state)}`,
        onclick: () => open(s.id),
      },
      h("span", { class: `src src--${s.source}`, "aria-hidden": "true", title: sourceLabel(s.source) }, sourceMark(s.source)),
      h(
        "span",
        { class: "row__main" },
        h(
          "span",
          { class: "row__top" },
          h("span", { class: "row__title" }, clip(s.title || s.native_id, 60)),
          h("span", { class: "row__age" }, age(s.updated_at)),
        ),
        h(
          "span",
          { class: "row__sub" },
          h("span", { class: `chip chip--${state}` }, stateLabel(state)),
          tail ? h("span", { class: "row__cwd" }, tail) : null,
        ),
        s.last_step ? h("span", { class: "row__step" }, clip(s.last_step, 90)) : null,
        rowGauge(s.usage),
      ),
    ),
  );
}

function emptyState(link: LinkStatus, on: ListHandlers): HTMLElement {
  if (link.state === "connected") {
    return h("p", { class: "empty" }, "Aucun agent en activité. L'œil veille.");
  }
  return h(
    "div",
    { class: "empty" },
    h("p", {}, link.message),
    h("button", { class: "btn btn--primary", onclick: on.settings }, "Ouvrir les réglages"),
  );
}

function onListKey(event: KeyboardEvent): void {
  if (event.key !== "ArrowDown" && event.key !== "ArrowUp") return;
  const rows = Array.from(document.querySelectorAll<HTMLButtonElement>("[data-row]"));
  const index = rows.indexOf(document.activeElement as HTMLButtonElement);
  const next = event.key === "ArrowDown" ? index + 1 : index - 1;
  const target = rows[Math.max(0, Math.min(rows.length - 1, next))];
  if (target) {
    event.preventDefault();
    target.focus();
  }
}

export function listView(sessions: Session[], link: LinkStatus, on: ListHandlers): HTMLElement {
  const sorted = [...sessions].sort((a, b) => {
    const wa = a.state === "waiting" || a.pending.length > 0 ? 0 : 1;
    const wb = b.state === "waiting" || b.pending.length > 0 ? 0 : 1;
    return wa - wb || Date.parse(b.updated_at) - Date.parse(a.updated_at);
  });
  const body =
    sorted.length === 0
      ? emptyState(link, on)
      : h("ul", { class: "list", "aria-label": "Agents", onkeydown: onListKey as EventListener }, ...sorted.map((s) => row(s, on.open)));
  return h("section", { class: "panel" }, header(link, on), h("div", { class: "panel__body" }, body));
}
