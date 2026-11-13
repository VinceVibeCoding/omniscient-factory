// Pending cards: permission, question, budget, external approval.
import type { Answer } from "../api";
import { h } from "../dom";
import { preview, secondsLeft, visibleText } from "../format";
import type { Pending } from "../types";

export interface PendingHandlers {
  answer(pending: Pending, answer: Answer): void;
  jump(): void;
}

/** Which session is asking: shown on every permission card (M1). */
export interface PendingContext {
  title: string;
  cwd: string | null;
}

const DECISION_LABEL: Record<string, string> = {
  allow: "Autoriser",
  deny: "Refuser",
  always: "Toujours (cette session)",
};

const ANSWER_MAX = 4000; // same bound as the Rust command and the daemon
const PREVIEW_MAX = 64 * 1024; // the daemon's preview cap: beyond, answer in the terminal
const TOO_LONG = "Trop long : réponds dans le terminal.";

interface Question {
  question: string;
  header: string;
  multi: boolean;
  options: string[];
}

function deadline(pending: Pending): HTMLElement | null {
  const left = secondsLeft(pending.expires_at);
  if (left === null) return null;
  const text =
    left > 0 ? `Sinon, l'agent demandera lui-même dans ${left} s` : "Délai écoulé";
  return h("p", { class: "card__deadline" }, text);
}

/** The full input as approved: never shortened silently, hidden characters made visible. */
function fullInput(p: Pending): { text: string; truncated: number | null } {
  const raw = p.payload.tool_input ?? p.payload.text ?? p.payload;
  const text = typeof raw === "string" ? raw : JSON.stringify(raw, null, 2) ?? "";
  const declared = typeof p.payload.truncated === "number" ? p.payload.truncated : null;
  if (text.length > PREVIEW_MAX) return { text: text.slice(0, PREVIEW_MAX), truncated: declared ?? text.length };
  return { text, truncated: declared };
}

function alwaysRules(p: Pending): string[] {
  const rules = p.payload.always_rules;
  return Array.isArray(rules) ? rules.filter((r): r is string => typeof r === "string") : [];
}

function decisionButton(p: Pending, o: string, disabled: boolean, on: PendingHandlers): HTMLElement {
  const kind = o === "deny" ? "btn--danger" : o === "allow" ? "btn--primary" : "";
  return h(
    "button",
    {
      class: `btn ${kind}`,
      disabled,
      title: disabled ? TOO_LONG : undefined,
      onclick: () => on.answer(p, { decision: o as "allow" | "deny" | "always" }),
    },
    DECISION_LABEL[o] ?? o,
  );
}

function permissionCard(p: Pending, ctx: PendingContext, on: PendingHandlers): HTMLElement {
  const order = ["allow", "always", "deny"];
  const options = order.filter((o) => p.options.length === 0 || p.options.includes(o));
  const { text, truncated } = fullInput(p);
  const rules = alwaysRules(p);
  const answerable = p.payload.answerable !== false;
  return h(
    "section",
    { class: "card card--permission", "aria-label": "Demande de permission" },
    h("h3", { class: "card__title" }, "Permission", p.tool ? h("code", {}, visibleText(p.tool)) : null),
    h("p", { class: "card__who" }, visibleText(ctx.title), ctx.cwd ? h("code", {}, visibleText(ctx.cwd)) : null),
    h("pre", { class: "card__preview card__preview--full", tabindex: 0 }, visibleText(text)),
    truncated !== null ? h("p", { class: "card__error", role: "alert" }, `Tronqué (${truncated} caractères). ${TOO_LONG}`) : null,
    options.includes("always") && rules.length > 0
      ? h("p", { class: "card__rule" }, "« Toujours » ajoute pour cette session : ", h("code", {}, visibleText(rules.join(", "))))
      : null,
    deadline(p),
    answerable
      ? h("div", { class: "card__actions" }, ...options.map((o) => decisionButton(p, o, truncated !== null && o !== "deny", on)))
      : h(
          "div",
          { class: "card__actions" },
          h("p", { class: "card__error" }, "Réponse non vérifiée pour cet agent : réponds dans le terminal."),
          h("button", { class: "btn btn--primary", onclick: () => on.jump() }, "Revenir"),
        ),
  );
}

function asQuestions(p: Pending): Question[] {
  const raw = p.payload.questions;
  if (Array.isArray(raw)) {
    return raw.flatMap((q): Question[] => {
      if (typeof q !== "object" || q === null) return [];
      const rec = q as Record<string, unknown>;
      if (typeof rec.question !== "string") return [];
      const opts = Array.isArray(rec.options) ? rec.options : [];
      return [
        {
          question: rec.question,
          header: typeof rec.header === "string" ? rec.header : "",
          multi: rec.multiSelect === true,
          options: opts.flatMap((o) =>
            typeof o === "string"
              ? [o]
              : typeof o === "object" && o !== null && typeof (o as { label?: unknown }).label === "string"
                ? [(o as { label: string }).label]
                : [],
          ),
        },
      ];
    });
  }
  // AgentRun question: free text (no options), sent as {answers: {question: text}}.
  const text = typeof p.payload.question === "string" && p.payload.question ? p.payload.question : "Question";
  return [{ question: text, header: "", multi: false, options: [...p.options] }];
}

function questionField(q: Question, index: number): HTMLElement {
  const name = `q${index}`;
  const legend = h("legend", {}, q.header ? `${q.header} — ${q.question}` : q.question);
  if (q.options.length === 0) {
    return h(
      "fieldset",
      { class: "field" },
      legend,
      h("textarea", { name, rows: 3, maxlength: ANSWER_MAX, "aria-label": q.question, required: true }),
    );
  }
  const type = q.multi ? "checkbox" : "radio";
  return h(
    "fieldset",
    { class: "field" },
    legend,
    ...q.options.map((label, i) =>
      h(
        "label",
        { class: "choice" },
        h("input", { type, name, value: label, checked: !q.multi && i === 0 }),
        h("span", {}, label),
      ),
    ),
  );
}

function readAnswers(form: HTMLFormElement, questions: Question[]): Record<string, string> | null {
  const data = new FormData(form);
  const answers: Record<string, string> = {};
  for (const [i, q] of questions.entries()) {
    const values = data
      .getAll(`q${i}`)
      .map((v) => String(v).trim())
      .filter(Boolean);
    if (values.length === 0) return null;
    answers[q.question] = values.join(", "); // multi-select: labels joined with ", "
  }
  return answers;
}

function questionCard(p: Pending, on: PendingHandlers): HTMLElement {
  const questions = asQuestions(p);
  const error = h("p", { class: "card__error", role: "alert", hidden: true });
  const form = h(
    "form",
    { class: "card card--question", "aria-label": "Question de l'agent" },
    h("h3", { class: "card__title" }, "Question"),
    ...questions.map(questionField),
    deadline(p),
    error,
    h("div", { class: "card__actions" }, h("button", { class: "btn btn--primary", type: "submit" }, "Envoyer")),
  );
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const answers = readAnswers(form, questions);
    if (!answers) {
      error.textContent = "Répondez à chaque question.";
      error.hidden = false;
      return;
    }
    on.answer(p, { answers });
  });
  return form;
}

function budgetCard(p: Pending, on: PendingHandlers): HTMLElement {
  const rows = Object.entries(p.payload).map(([k, v]) =>
    h("div", { class: "kv" }, h("dt", {}, k), h("dd", {}, preview(v, 80))),
  );
  return h(
    "section",
    { class: "card card--budget", "aria-label": "Budget atteint" },
    h("h3", { class: "card__title" }, "Budget atteint"),
    h("dl", { class: "card__kv" }, ...rows),
    h(
      "div",
      { class: "card__actions" },
      h("button", { class: "btn btn--primary", onclick: () => on.answer(p, { choice: "raise" }) }, "Augmenter le plafond"),
      h("button", { class: "btn btn--danger", onclick: () => on.answer(p, { choice: "stop" }) }, "Arrêter"),
    ),
  );
}

function externalCard(p: Pending, on: PendingHandlers): HTMLElement {
  return h(
    "section",
    { class: "card card--external", "aria-label": "Approbation externe" },
    h("h3", { class: "card__title" }, "Approbation requise"),
    h("p", {}, "Cette approbation se fait dans la console de l'agent."),
    p.tool ? h("p", {}, h("code", {}, p.tool)) : null,
    h("div", { class: "card__actions" }, h("button", { class: "btn btn--primary", onclick: () => on.jump() }, "Ouvrir la console")),
  );
}

export function pendingCard(p: Pending, ctx: PendingContext, on: PendingHandlers): HTMLElement {
  switch (p.kind) {
    case "permission":
      return permissionCard(p, ctx, on);
    case "question":
      return questionCard(p, on);
    case "budget":
      return budgetCard(p, on);
    case "approval_external":
      return externalCard(p, on);
    default:
      return h("section", { class: "card" }, h("pre", { class: "card__preview" }, preview(p.payload)));
  }
}
