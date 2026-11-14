// State and actions of the Chat tab (mode A). Owns its own immutable model and calls
// `changed()` so main.ts re-renders. Streamed `chat` events are appended at once, then
// the conversation is re-read from the daemon when a turn ends (source of truth).
import * as osdApi from "./api-osd";
import type { Chat, ChatEvent, ChatMessage } from "./types";
import type { ChatHandlers, ChatModel } from "./views/chat";

const RELOAD_DEBOUNCE_MS = 300;

export class ChatController {
  private model: ChatModel = {
    projects: [],
    chats: [],
    project: "",
    activeId: null,
    active: null,
    messages: [],
    draft: "",
    busy: false,
  };
  private reloadTimer: number | undefined;
  readonly handlers: ChatHandlers;

  constructor(
    private readonly changed: () => void,
    private readonly fail: (err: unknown) => void,
  ) {
    this.handlers = {
      open: (id) => void this.open(id),
      close: () => this.set({ activeId: null, active: null, messages: [], draft: "" }),
      setProject: (project) => this.set({ project }),
      setDraft: (draft) => {
        this.model = { ...this.model, draft }; // no re-render while typing
      },
      send: (text) => void this.send(text),
      stop: () => void this.stop(),
      refresh: () => void this.load(),
    };
  }

  get view(): ChatModel {
    return this.model;
  }

  private set(patch: Partial<ChatModel>): void {
    this.model = { ...this.model, ...patch };
    this.changed();
  }

  private async guard(work: () => Promise<void>): Promise<void> {
    try {
      await work();
    } catch (err) {
      this.set({ busy: false });
      this.fail(err);
    }
  }

  /** Projects + conversation list (called when the Chat tab opens). */
  async load(): Promise<void> {
    await this.guard(async () => {
      const [projects, chats] = await Promise.all([osdApi.listProjects(), osdApi.chatList()]);
      const project = projects.some((p) => p.name === this.model.project) ? this.model.project : (projects[0]?.name ?? "");
      const active = chats.find((c) => c.chat_id === this.model.activeId) ?? this.model.active;
      this.set({ projects, chats, project, active });
    });
  }

  private async open(chatId: string): Promise<void> {
    this.set({ activeId: chatId, messages: [], draft: "" });
    await this.reload();
  }

  private async reload(): Promise<void> {
    const id = this.model.activeId;
    if (!id) return;
    await this.guard(async () => {
      const { chat, messages } = await osdApi.chatGet(id);
      if (this.model.activeId !== id) return;
      this.set({ active: chat ?? this.model.active, messages });
    });
  }

  private scheduleReload(): void {
    window.clearTimeout(this.reloadTimer);
    this.reloadTimer = window.setTimeout(() => {
      void this.reload();
      void this.refreshList();
    }, RELOAD_DEBOUNCE_MS);
  }

  private async refreshList(): Promise<void> {
    await this.guard(async () => this.set({ chats: await osdApi.chatList() }));
  }

  private userMessage(text: string): ChatMessage {
    return { role: "user", text, at: new Date().toISOString() };
  }

  private async send(text: string): Promise<void> {
    const id = this.model.activeId;
    this.set({ busy: true, draft: "", messages: [...this.model.messages, this.userMessage(text)] });
    await this.guard(async () => {
      if (id) {
        await osdApi.chatSend(id, text);
      } else {
        if (!this.model.project) throw new Error("Choisissez un projet.");
        const chatId = await osdApi.chatCreate(this.model.project, text);
        const now = new Date().toISOString();
        const active: Chat = { chat_id: chatId, project: this.model.project, title: text.slice(0, 80), state: "running", updated_at: now };
        this.set({ activeId: chatId, active });
      }
      this.set({ busy: false });
    });
  }

  private async stop(): Promise<void> {
    const id = this.model.activeId;
    if (id) await this.guard(async () => void (await osdApi.chatStop(id)));
  }

  private withState(event: ChatEvent): Partial<ChatModel> {
    const state = event.state;
    if (!state) return {};
    const chats = this.model.chats.map((c) => (c.chat_id === event.chat_id ? { ...c, state, updated_at: event.at || c.updated_at } : c));
    const active = this.model.active?.chat_id === event.chat_id ? { ...this.model.active, state } : this.model.active;
    return { chats, active };
  }

  private messageFor(event: ChatEvent): ChatMessage | null {
    const at = event.at || new Date().toISOString();
    if (event.kind === "text" && event.text) return { role: "assistant", text: event.text, at };
    if (event.kind === "tool") return { role: "tool", text: event.text ?? "", at, ...(event.tool ? { tool: event.tool } : {}) };
    if (event.kind === "error") return { role: "error", text: event.text ?? "Erreur inconnue.", at };
    return null;
  }

  /** SSE `chat` event from the daemon. */
  onEvent(event: ChatEvent): void {
    const patch = this.withState(event);
    const message = event.chat_id === this.model.activeId ? this.messageFor(event) : null;
    this.set(message ? { ...patch, messages: [...this.model.messages, message] } : patch);
    const known = this.model.chats.some((c) => c.chat_id === event.chat_id);
    if (event.kind === "result" || event.kind === "error" || event.kind === "state" || !known) this.scheduleReload();
  }
}
