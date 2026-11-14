// « Affichage » section of the Réglages tab: skin, OSD view and ticker — the same
// choices as the right-click menu. Each change is applied at once (PUT /config); a key
// set in ~/.claude/settings.json (env OMNIGROUP_*) is locked and shown disabled.
import { h } from "../dom";
import type { ConfigPatch, ConfigView, OsdView, Skin } from "../types";

const SKINS: ReadonlyArray<readonly [Skin, string, string]> = [
  ["default", "Default", "palette de la maison Omniscient"],
  ["terminal", "Terminal", "vert sur noir, façon htop"],
  ["dashboard", "Dashboard", "bleu froid façon Bloomberg"],
];
const VIEWS: ReadonlyArray<readonly [OsdView, string]> = [
  ["bars", "Barres (avec ticker)"],
  ["gauge", "Jauges circulaires"],
];
const LOCK_NOTE = " (settings.json)";

function radios<T extends string>(
  name: string,
  legend: string,
  options: ReadonlyArray<readonly [T, string, string?]>,
  current: T,
  locked: boolean,
  pick: (value: T) => void,
): HTMLElement {
  return h(
    "fieldset",
    { class: "field", disabled: locked },
    h("legend", {}, legend + (locked ? LOCK_NOTE : "")),
    ...options.map(([value, label, hint]) => {
      const input = h("input", { type: "radio", name, value });
      input.checked = value === current;
      input.addEventListener("change", () => input.checked && pick(value));
      return h("label", { class: "choice" }, input, h("span", {}, label), hint ? h("span", { class: "setting__hint" }, hint) : null);
    }),
  );
}

export function displaySection(view: ConfigView | null, change: (patch: ConfigPatch) => void): HTMLElement {
  if (!view) {
    return h("section", { class: "settings" }, h("h2", { class: "section-title" }, "Affichage"), h("p", { class: "setting__hint" }, "Réglages d'affichage indisponibles (démon hors ligne)."));
  }
  const { config, sources } = view;
  const ticker = h("input", { type: "checkbox", name: "show_ticker" });
  ticker.checked = config.show_ticker;
  ticker.disabled = sources.show_ticker === "settings";
  ticker.addEventListener("change", () => change({ show_ticker: ticker.checked }));
  return h(
    "section",
    { class: "settings", "aria-label": "Affichage" },
    h("h2", { class: "section-title" }, "Affichage"),
    radios("theme", "Thème", SKINS, config.theme, sources.theme === "settings", (theme) => change({ theme })),
    radios("osd_view", "Vue de l'OSD", VIEWS, config.osd_view, sources.osd_view === "settings", (osd_view) => change({ osd_view })),
    h("label", { class: "choice" }, ticker, h("span", {}, "Afficher le ticker des coûts" + (ticker.disabled ? LOCK_NOTE : ""))),
    h("p", { class: "setting__hint" }, "Aussi : clic droit sur l'OSD, « omniscient group config set … » ou /omnigroup dans Claude Code."),
  );
}
