// Settings / pairing panel. The widget token never reaches this page: pairing only
// sends the 6-digit code shown on the Windows desktop, Rust keeps the token.
import { h } from "../dom";
import type { LinkStatus, Settings } from "../types";

export interface SettingsHandlers {
  back(): void;
  save(settings: Settings): void;
  pairStart(): void;
  pairFinish(code: string): void;
  unpair(): void;
}

function field(label: string, input: HTMLInputElement, hint?: string): HTMLElement {
  return h("label", { class: "setting" }, h("span", { class: "setting__label" }, label), input, hint ? h("span", { class: "setting__hint" }, hint) : null);
}

function pairingForm(on: SettingsHandlers): HTMLElement {
  const code = h("input", {
    name: "code",
    inputmode: "numeric",
    autocomplete: "one-time-code",
    maxlength: 6,
    pattern: "[0-9]{6}",
    spellcheck: "false",
    placeholder: "123456",
  });
  const form = h(
    "form",
    { class: "settings" },
    h("p", { class: "setting__hint" }, "1. Demandez un code : il s'affiche sur le bureau Windows (2 minutes, 5 essais)."),
    h("div", { class: "card__actions" }, h("button", { class: "btn", type: "button", onclick: () => on.pairStart() }, "Demander un code")),
    field("2. Code affiché", code),
    h(
      "div",
      { class: "card__actions" },
      h("button", { class: "btn btn--primary", type: "submit" }, "Coupler"),
      h("button", { class: "btn btn--ghost", type: "button", onclick: () => on.unpair() }, "Oublier le couplage"),
    ),
    h(
      "p",
      { class: "setting__hint" },
      "Pas de code sur le bureau ? Voir « omniscient group pair » dans un terminal que vous ouvrez vous-même.",
    ),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    on.pairFinish(code.value.trim());
  });
  return form;
}

export function settingsView(current: Settings, link: LinkStatus, on: SettingsHandlers): HTMLElement {
  const url = h("input", { name: "daemon_url", value: current.daemon_url, required: true, spellcheck: "false" });
  const form = h(
    "form",
    { class: "settings" },
    h("p", { class: `notice notice--${link.state}`, role: "status" }, link.message),
    field("Adresse du démon", url, "Local uniquement (127.0.0.1 ou localhost)."),
    h("div", { class: "card__actions" }, h("button", { class: "btn", type: "submit" }, "Enregistrer")),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    on.save({ daemon_url: url.value.trim() });
  });
  return h(
    "section",
    { class: "panel", "aria-label": "Réglages" },
    h(
      "header",
      { class: "bar" },
      h("button", { class: "icon-btn", "aria-label": "Retour", title: "Retour (Échap)", onclick: on.back }, "←"),
      h("h1", { class: "bar__title" }, "Réglages"),
    ),
    h("div", { class: "panel__body" }, form, h("h2", { class: "section-title" }, "Couplage"), pairingForm(on)),
  );
}
