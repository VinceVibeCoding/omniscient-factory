// Settings / pairing panel.
import { h } from "../dom";
import type { LinkStatus, Settings } from "../types";

export interface SettingsHandlers {
  back(): void;
  save(settings: Settings): void;
  pair(): void;
}

function field(label: string, input: HTMLInputElement, hint?: string): HTMLElement {
  return h("label", { class: "setting" }, h("span", { class: "setting__label" }, label), input, hint ? h("span", { class: "setting__hint" }, hint) : null);
}

export function settingsView(current: Settings, link: LinkStatus, on: SettingsHandlers): HTMLElement {
  const url = h("input", { name: "daemon_url", value: current.daemon_url, required: true, spellcheck: "false" });
  const distro = h("input", { name: "wsl_distro", value: current.wsl_distro, spellcheck: "false", placeholder: "distribution par défaut" });
  const command = h("input", { name: "token_command", value: current.token_command, required: true, spellcheck: "false" });
  const token = h("input", { name: "token", type: "password", value: current.token, autocomplete: "off", spellcheck: "false" });
  const read = (): Settings => ({
    daemon_url: url.value.trim(),
    wsl_distro: distro.value.trim(),
    token_command: command.value.trim(),
    token: token.value.trim(),
  });
  const form = h(
    "form",
    { class: "settings" },
    h("p", { class: `notice notice--${link.state}`, role: "status" }, link.message),
    field("Adresse du démon", url, "Local uniquement (127.0.0.1 ou localhost)."),
    field("Distribution WSL", distro, "Vide = distribution par défaut."),
    field("Commande du jeton", command, "Lancée dans WSL : omniscient group token"),
    h(
      "div",
      { class: "card__actions" },
      h("button", { class: "btn btn--primary", type: "button", onclick: () => on.pair() }, "Coupler via WSL"),
    ),
    field("Jeton (ou collez-le ici)", token),
    h(
      "div",
      { class: "card__actions" },
      h("button", { class: "btn", type: "submit" }, "Enregistrer"),
    ),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    on.save(read());
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
    h("div", { class: "panel__body" }, form),
  );
}
