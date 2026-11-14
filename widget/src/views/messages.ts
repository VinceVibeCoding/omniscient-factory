// Conversation log + composer shared by the widget chat (mode A) and a running session's
// transcript (mode B). The composer is not a <form>: main.ts keeps rendering while it has
// focus and restores its text and caret through `data-keep`, so streamed replies show up
// while the owner types. Enter sends, Shift+Enter adds a line.
import { h } from "../dom";
import { age, clip } from "../format";
import type { ChatMessage } from "../types";

const MAX_TEXT = 20_000;
const SHOWN_CHARS = 8000;

const ROLE_LABEL: Record<ChatMessage["role"], string> = {
  user: "Vous",
  assistant: "Claude",
  tool: "Outil",
  error: "Erreur",
};

function bubble(m: ChatMessage): HTMLElement {
  const who = m.role === "tool" && m.tool ? `${ROLE_LABEL.tool} · ${m.tool}` : ROLE_LABEL[m.role];
  return h(
    "li",
    { class: `msg msg--${m.role}` },
    h("div", { class: "msg__head" }, h("span", { class: "msg__who" }, who), m.at ? h("time", { datetime: m.at }, age(m.at)) : null),
    m.text ? h("p", { class: "msg__text" }, clip(m.text, SHOWN_CHARS)) : null,
  );
}

export function messageLog(messages: readonly ChatMessage[], empty: string, key: string): HTMLElement {
  if (messages.length === 0) return h("p", { class: "empty chat__empty" }, empty);
  return h("ol", { class: "chat__log", "data-scroll": key, "aria-label": "Messages" }, ...messages.map(bubble));
}

export interface ComposerOptions {
  key: string; // data-keep id: text + caret survive re-renders
  placeholder: string;
  label: string;
  draft: string;
  busy: boolean;
  onDraft(text: string): void;
  onSend(text: string): void;
}

export function composer(o: ComposerOptions): HTMLElement {
  const area = h("textarea", {
    class: "composer__input",
    rows: 2,
    maxlength: MAX_TEXT,
    placeholder: o.placeholder,
    "aria-label": o.label,
    "data-keep": o.key,
  });
  area.value = o.draft;
  const send = (): void => {
    const text = area.value.trim();
    if (!text || o.busy) return;
    area.value = "";
    o.onDraft("");
    o.onSend(text);
  };
  area.addEventListener("input", () => o.onDraft(area.value));
  area.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !event.isComposing) {
      event.preventDefault();
      send();
    }
  });
  return h(
    "div",
    { class: "composer" },
    area,
    h("button", { class: "btn btn--primary", type: "button", disabled: o.busy, onclick: send }, "Envoyer"),
  );
}
