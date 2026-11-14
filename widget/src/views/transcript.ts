// Chat with a running session (mode B): its recent conversation, read from the session's
// transcript by the daemon, plus a reply box when the daemon can type into it (terminal
// sessions driven through herdr). IDE sessions (VS Code…) cannot receive text from
// outside: the widget still shows them and answers their approvals and questions.
import { h } from "../dom";
import type { Transcript } from "../types";
import { composer, messageLog } from "./messages";

export interface TranscriptHandlers {
  setDraft(text: string): void;
  send(text: string): void;
}

export function transcriptSection(
  sessionId: string,
  t: Transcript | null,
  draft: string,
  on: TranscriptHandlers,
): HTMLElement {
  const children: HTMLElement[] = [h("h2", { class: "section-title" }, "Conversation")];
  if (t === null) {
    children.push(h("p", { class: "setting__hint" }, "Conversation indisponible pour cette session."));
  } else {
    children.push(messageLog(t.messages.slice(-50), "Aucun message pour l'instant.", `session-${sessionId}`));
    children.push(
      t.can_prompt
        ? composer({
            key: `session-${sessionId}`,
            placeholder: "Écrire à cette session…",
            label: "Message à la session",
            draft,
            busy: false,
            onDraft: on.setDraft,
            onSend: on.send,
          })
        : h(
            "p",
            { class: "notice" },
            "Cette session tourne dans un IDE : on ne peut pas lui écrire depuis l'extérieur. Répondez dans l'IDE ; ses autorisations et questions arrivent ici.",
          ),
    );
  }
  return h("section", { class: "transcript", "aria-label": "Conversation de la session" }, ...children);
}
