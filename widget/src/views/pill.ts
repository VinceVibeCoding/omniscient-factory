// Collapsed pill: eye mark + counts. Click or Enter expands the panel.
import { h } from "../dom";
import { eyeMark } from "../eye";
import type { LinkStatus, Session, UsageTotals } from "../types";
import { totalsText } from "./usage";

export interface Counts {
  working: number;
  waiting: number;
  done: number;
}

export function countSessions(sessions: Session[]): Counts {
  return sessions.reduce(
    (acc, s) => ({
      working: acc.working + (s.state === "working" ? 1 : 0),
      waiting: acc.waiting + (s.state === "waiting" || s.pending.length > 0 ? 1 : 0),
      done: acc.done + (s.state === "done" ? 1 : 0),
    }),
    { working: 0, waiting: 0, done: 0 },
  );
}

function online(link: LinkStatus): boolean {
  return link.state === "connected";
}

export function pillView(
  sessions: Session[],
  link: LinkStatus,
  totals: UsageTotals | null,
  expand: () => void,
): HTMLElement {
  const c = countSessions(sessions);
  const total = online(link) ? totalsText(totals) : null;
  const label = online(link)
    ? `Omnigroup : ${c.working} en cours, ${c.waiting} en attente, ${c.done} terminés${total ? `, ${total}` : ""}. Ouvrir le panneau.`
    : `Omnigroup : ${link.message} Ouvrir le panneau.`;
  return h(
    "button",
    {
      class: `pill ${c.waiting > 0 ? "pill--alert" : ""} ${online(link) ? "" : "pill--offline"}`,
      "aria-label": label,
      title: label,
      onclick: expand,
    },
    eyeMark(26, c.working > 0),
    online(link)
      ? h(
          "span",
          { class: "pill__text", "aria-hidden": "true" },
          h(
            "span",
            { class: "pill__counts" },
            h("span", { class: "count count--working" }, `● ${c.working}`),
            h("span", { class: "count count--waiting" }, `◆ ${c.waiting}`),
            h("span", { class: "count count--done" }, `✓ ${c.done}`),
          ),
          total ? h("span", { class: "pill__total" }, total) : null,
        )
      : h("span", { class: "pill__offline", "aria-hidden": "true" }, "hors ligne"),
  );
}
