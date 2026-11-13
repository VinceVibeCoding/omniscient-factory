// Session detail: actions, pending cards, optional prompt box, timeline.
import type { Answer } from "../api";
import { h } from "../dom";
import { age, clip, cwdTail, sourceLabel, sourceMark, stateLabel, stepLabel } from "../format";
import type { Pending, SessionDetail, Step } from "../types";
import { pendingCard } from "./pending";
import { usageBlock } from "./usage";

export interface DetailHandlers {
  back(): void;
  answer(pending: Pending, answer: Answer): void;
  jump(id: string): void;
  dismiss(id: string): void;
  prompt(id: string, text: string): void;
}

function timelineItem(step: Step): HTMLElement {
  const body: (HTMLElement | string)[] = [
    h(
      "div",
      { class: "step__head" },
      h("span", { class: `step__kind step__kind--${step.kind}` }, stepLabel(step.kind)),
      h("time", { datetime: step.at, class: "step__age" }, age(step.at)),
    ),
    h("p", { class: "step__summary" }, step.summary),
  ];
  if (step.detail) {
    body.push(
      h("details", { class: "step__detail" }, h("summary", {}, "Détails"), h("pre", {}, clip(step.detail, 4000))),
    );
  }
  return h("li", { class: `step step--${step.kind}` }, ...body);
}

function promptBox(s: SessionDetail, on: DetailHandlers): HTMLElement {
  const area = h("textarea", {
    rows: 2,
    maxlength: 4000,
    placeholder: "Écrire à l'agent…",
    "aria-label": "Message à l'agent",
  });
  const form = h(
    "form",
    { class: "prompt" },
    area,
    h("button", { class: "btn btn--primary", type: "submit" }, "Envoyer un message"),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const text = area.value.trim();
    if (text) on.prompt(s.id, text);
  });
  return form;
}

function actions(s: SessionDetail, on: DetailHandlers): HTMLElement {
  return h(
    "div",
    { class: "actions" },
    s.jump
      ? h(
          "button",
          { class: "btn", title: s.jump.label, onclick: () => on.jump(s.id) },
          "Revenir",
        )
      : null,
    h("button", { class: "btn btn--ghost", onclick: () => on.dismiss(s.id) }, "Rayer"),
  );
}

export function detailView(s: SessionDetail, on: DetailHandlers): HTMLElement {
  const open = s.pending.filter((p) => p.status === "open");
  const steps = [...s.steps].sort((a, b) => Date.parse(b.at) - Date.parse(a.at));
  const handlers = { answer: on.answer, jump: () => on.jump(s.id) };
  return h(
    "section",
    { class: "panel", "aria-label": `Détail : ${s.title}` },
    h(
      "header",
      { class: "bar" },
      h("button", { class: "icon-btn", "aria-label": "Retour à la liste", title: "Retour (Échap)", onclick: on.back }, "←"),
      h("span", { class: `src src--${s.source}`, title: sourceLabel(s.source), "aria-hidden": "true" }, sourceMark(s.source)),
      h("h1", { class: "bar__title", title: s.title }, clip(s.title || s.native_id, 40)),
      h("span", { class: `chip chip--${s.state}` }, stateLabel(s.state)),
    ),
    h(
      "div",
      { class: "panel__body" },
      s.cwd ? h("p", { class: "meta", title: s.cwd }, cwdTail(s.cwd)) : null,
      actions(s, on),
      ...open.map((p) => pendingCard(p, handlers)),
      s.state === "idle" ? promptBox(s, on) : null,
      usageBlock(s.usage),
      h("h2", { class: "section-title" }, "Étapes"),
      steps.length === 0
        ? h("p", { class: "empty" }, "Aucune étape pour l'instant.")
        : h("ol", { class: "timeline", "aria-label": "Étapes" }, ...steps.map(timelineItem)),
    ),
  );
}
