// Chat tab (mode A): talk to Claude Code from the widget. The daemon runs a headless
// Claude session in the chosen project and streams its replies; tool permissions still
// arrive as approvals in the Sessions tab.
import { h } from "../dom";
import { age, clip } from "../format";
import type { Chat, ChatMessage, ChatState, Project } from "../types";
import { composer, messageLog } from "./messages";

export interface ChatModel {
  projects: readonly Project[];
  chats: readonly Chat[];
  project: string;
  activeId: string | null;
  active: Chat | null;
  messages: readonly ChatMessage[];
  draft: string;
  busy: boolean;
}

export interface ChatHandlers {
  open(chatId: string): void;
  close(): void;
  setProject(name: string): void;
  setDraft(text: string): void;
  send(text: string): void;
  stop(): void;
  refresh(): void;
}

const STATE_LABEL: Record<ChatState, string> = { running: "en cours", idle: "prête", error: "erreur" };
const CHIP: Record<ChatState, string> = { running: "working", idle: "idle", error: "error" };

function projectPicker(m: ChatModel, on: ChatHandlers): HTMLElement {
  const select = h("select", { class: "chat__project", "aria-label": "Projet" });
  if (m.projects.length === 0) select.append(h("option", { value: "" }, "Aucun projet"));
  for (const p of m.projects) {
    const option = h("option", { value: p.name, title: p.path_label }, p.path_label ? `${p.name} — ${p.path_label}` : p.name);
    option.selected = p.name === m.project;
    select.append(option);
  }
  select.addEventListener("change", () => on.setProject(select.value));
  return h("label", { class: "chat__picker" }, h("span", { class: "setting__label" }, "Projet"), select);
}

function chatRow(c: Chat, on: ChatHandlers): HTMLElement {
  return h(
    "li",
    {},
    h(
      "button",
      { class: "row chat__row", type: "button", "data-row": c.chat_id, onclick: () => on.open(c.chat_id) },
      h(
        "span",
        { class: "row__main" },
        h("span", { class: "row__top" }, h("span", { class: "row__title" }, clip(c.title, 60)), h("span", { class: "row__age" }, age(c.updated_at))),
        h("span", { class: "row__sub" }, h("span", { class: `chip chip--${CHIP[c.state]}` }, STATE_LABEL[c.state]), h("span", { class: "row__cwd" }, c.project)),
      ),
    ),
  );
}

function home(m: ChatModel, on: ChatHandlers): (HTMLElement | null)[] {
  return [
    h("p", { class: "setting__hint" }, "Nouvelle conversation avec Claude Code dans le projet choisi."),
    projectPicker(m, on),
    composer({
      key: "chat-new",
      placeholder: "Demander quelque chose à Claude…",
      label: "Premier message",
      draft: m.draft,
      busy: m.busy || m.projects.length === 0,
      onDraft: on.setDraft,
      onSend: on.send,
    }),
    h(
      "div",
      { class: "chat__list-head" },
      h("h2", { class: "section-title" }, "Conversations"),
      h("button", { class: "icon-btn", type: "button", "aria-label": "Actualiser", title: "Actualiser", onclick: on.refresh }, "↻"),
    ),
    m.chats.length === 0
      ? h("p", { class: "empty" }, "Aucune conversation pour l'instant.")
      : h("ul", { class: "list" }, ...m.chats.map((c) => chatRow(c, on))),
  ];
}

function conversation(m: ChatModel, on: ChatHandlers): (HTMLElement | null)[] {
  const running = m.active?.state === "running";
  return [
    h(
      "div",
      { class: "chat__bar" },
      h("button", { class: "btn btn--ghost", type: "button", onclick: on.close }, "← Conversations"),
      m.active ? h("span", { class: `chip chip--${CHIP[m.active.state]}` }, STATE_LABEL[m.active.state]) : null,
      running ? h("button", { class: "btn btn--danger", type: "button", onclick: on.stop }, "Arrêter") : null,
    ),
    m.active ? h("p", { class: "meta" }, `${clip(m.active.title, 80)} · ${m.active.project}`) : null,
    messageLog(m.messages, "Envoi en cours…", "chat"),
    composer({
      key: `chat-${m.activeId ?? ""}`,
      placeholder: running ? "Claude répond… (vous pouvez préparer la suite)" : "Répondre…",
      label: "Message à Claude",
      draft: m.draft,
      busy: m.busy || running,
      onDraft: on.setDraft,
      onSend: on.send,
    }),
  ];
}

export function chatView(m: ChatModel, on: ChatHandlers, head: HTMLElement): HTMLElement {
  const body = m.activeId ? conversation(m, on) : home(m, on);
  return h("section", { class: "panel panel--chat", "aria-label": "Chat" }, head, h("div", { class: "panel__body chat" }, ...body));
}
