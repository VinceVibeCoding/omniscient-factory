// Usage and context: row gauge, detail "Consommation" block, pill total. Everything
// hides when the daemon has no usage for a session.
import { h } from "../dom";
import { clock, compactTokens, dollars, level } from "../format";
import type { Usage, UsageTotals } from "../types";

function isNum(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

/** Context fill in %, or null when unknown. */
export function contextPct(usage: Usage | null | undefined): number | null {
  if (!usage || !isNum(usage.context_tokens) || !isNum(usage.context_limit) || usage.context_limit <= 0) {
    return null;
  }
  return Math.min(100, Math.max(0, (usage.context_tokens / usage.context_limit) * 100));
}

function bar(pct: number, label: string, extraClass = ""): HTMLElement {
  const rounded = Math.round(pct);
  const fill = h("span", { class: `gauge__fill gauge__fill--${level(pct)}` });
  fill.style.width = `${pct}%`; // CSSOM, allowed by the CSP
  return h(
    "span",
    {
      class: `gauge ${extraClass}`,
      role: "meter",
      "aria-valuemin": 0,
      "aria-valuemax": 100,
      "aria-valuenow": rounded,
      "aria-label": label,
    },
    fill,
  );
}

/** Slim gauge for a list row: « contexte 42 % ». */
export function rowGauge(usage: Usage | null | undefined): HTMLElement | null {
  const pct = contextPct(usage);
  if (pct === null) return null;
  const label = `contexte ${Math.round(pct)} %`;
  return h("span", { class: "row__gauge" }, bar(pct, label, "gauge--slim"), h("span", { class: `gauge__label gauge__label--${level(pct)}` }, label));
}

function tokensLine(usage: Usage): HTMLElement {
  const parts = [
    `entrée ${compactTokens(usage.input_tokens)}`,
    `sortie ${compactTokens(usage.output_tokens)}`,
  ];
  const cache = (usage.cache_read_tokens || 0) + (usage.cache_write_tokens || 0);
  if (cache > 0) {
    parts.push(`cache ${compactTokens(usage.cache_read_tokens || 0)} lus / ${compactTokens(usage.cache_write_tokens || 0)} écrits`);
  }
  return h("p", { class: "usage__tokens" }, parts.join(" · "));
}

function planLine(usage: Usage): HTMLElement | null {
  if (!isNum(usage.plan_used_pct)) return null;
  const pct = Math.min(100, Math.max(0, usage.plan_used_pct));
  const reset = clock(usage.plan_resets_at);
  const label = `Forfait ${Math.round(pct)} %${reset ? ` · réinitialisé à ${reset}` : ""}`;
  return h("div", { class: "usage__row" }, h("span", { class: "usage__label" }, label), bar(pct, label));
}

/** Detail panel block « Consommation ». */
export function usageBlock(usage: Usage | null | undefined): HTMLElement | null {
  if (!usage) return null;
  const pct = contextPct(usage);
  const context =
    pct === null || !isNum(usage.context_tokens) || !isNum(usage.context_limit)
      ? null
      : h(
          "div",
          { class: "usage__row" },
          h(
            "span",
            { class: "usage__label" },
            `Contexte ${compactTokens(usage.context_tokens)} / ${compactTokens(usage.context_limit)} jetons`,
          ),
          bar(pct, `contexte ${Math.round(pct)} %`),
        );
  return h(
    "section",
    { class: "usage", "aria-label": "Consommation" },
    h(
      "h2",
      { class: "section-title" },
      "Consommation",
      usage.model ? h("span", { class: "usage__model" }, usage.model) : null,
    ),
    context,
    tokensLine(usage),
    isNum(usage.cost_usd) ? h("p", { class: "usage__cost" }, `Coût ${dollars(usage.cost_usd)}`) : null,
    planLine(usage),
  );
}

/** Pill total: « 1,2 M jetons · 0,84 $ ». */
export function totalsText(totals: UsageTotals | null): string | null {
  if (!totals) return null;
  const tokens = totals.input_tokens + totals.output_tokens;
  if (tokens <= 0 && !isNum(totals.cost_usd)) return null;
  const cost = isNum(totals.cost_usd) ? ` · ${dollars(totals.cost_usd)}` : "";
  return `${compactTokens(tokens)} jetons${cost}`;
}
