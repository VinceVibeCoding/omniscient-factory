// Typed wrappers for the OSD, config and chat commands. Every daemon payload is
// normalised here (untrusted JSON): views only ever see well-formed values.
import { invoke } from "@tauri-apps/api/core";
import type {
  Chat,
  ChatEvent,
  ChatMessage,
  ChatRole,
  ChatState,
  ConfigPatch,
  ConfigSource,
  ConfigView,
  Limit,
  Osd,
  OsdView,
  Project,
  Skin,
  TickerItem,
  Transcript,
} from "./types";

const SKINS: readonly Skin[] = ["default", "terminal", "dashboard"];
const VIEWS: readonly OsdView[] = ["bars", "gauge"];
const SOURCES: readonly ConfigSource[] = ["default", "config", "settings"];
const ROLES: readonly ChatRole[] = ["user", "assistant", "tool", "error"];
const CHAT_STATES: readonly ChatState[] = ["running", "idle", "error"];
const KINDS: readonly ChatEvent["kind"][] = ["start", "text", "tool", "result", "error", "state"];
const TICKER_MAX = 40;
const MESSAGES_MAX = 500;

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function str(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function num(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

function oneOf<T extends string>(value: unknown, allowed: readonly T[], fallback: T): T {
  return allowed.includes(value as T) ? (value as T) : fallback;
}

function list(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

function toLimit(raw: unknown): Limit | null {
  if (!isRecord(raw)) return null;
  const pct = num(raw.used_pct);
  if (pct === null) return null;
  return {
    id: str(raw.id, "limit"),
    label: str(raw.label, str(raw.id, "Limite")),
    used_pct: Math.min(100, Math.max(0, pct)),
    resets_at: typeof raw.resets_at === "string" ? raw.resets_at : null,
  };
}

function toTicker(raw: unknown): TickerItem | null {
  if (!isRecord(raw) || typeof raw.msg_id !== "string") return null;
  return {
    at: str(raw.at),
    msg_id: raw.msg_id,
    cost_usd: num(raw.cost_usd),
    tool: str(raw.tool),
    output_tokens: num(raw.output_tokens) ?? 0,
    model: str(raw.model),
  };
}

export function toOsd(raw: unknown): Osd | null {
  if (!isRecord(raw)) return null;
  const live = isRecord(raw.live) ? raw.live : {};
  return {
    limits: list(raw.limits).map(toLimit).filter((l): l is Limit => l !== null),
    live: { is_live: live.is_live === true, tokens_per_minute: num(live.tokens_per_minute) ?? 0 },
    subagents: Math.max(0, Math.round(num(raw.subagents) ?? 0)),
    ticker: list(raw.ticker)
      .map(toTicker)
      .filter((t): t is TickerItem => t !== null)
      .slice(-TICKER_MAX),
    today_cost_usd: num(raw.today_cost_usd),
    updated_at: str(raw.updated_at),
  };
}

export function toConfigView(raw: unknown): ConfigView | null {
  if (!isRecord(raw) || !isRecord(raw.config)) return null;
  const c = raw.config;
  const s = isRecord(raw.sources) ? raw.sources : {};
  return {
    config: {
      theme: oneOf(c.theme, SKINS, "default"),
      osd_view: oneOf(c.osd_view, VIEWS, "bars"),
      show_ticker: c.show_ticker !== false,
    },
    sources: {
      theme: oneOf(s.theme, SOURCES, "default"),
      osd_view: oneOf(s.osd_view, SOURCES, "default"),
      show_ticker: oneOf(s.show_ticker, SOURCES, "default"),
    },
  };
}

function toMessage(raw: unknown): ChatMessage | null {
  if (!isRecord(raw) || typeof raw.text !== "string") return null;
  const msg: ChatMessage = { role: oneOf(raw.role, ROLES, "assistant"), text: raw.text, at: str(raw.at) };
  return typeof raw.tool === "string" && raw.tool ? { ...msg, tool: raw.tool } : msg;
}

function toMessages(raw: unknown): ChatMessage[] {
  return list(raw)
    .map(toMessage)
    .filter((m): m is ChatMessage => m !== null)
    .slice(-MESSAGES_MAX);
}

function toChat(raw: unknown): Chat | null {
  if (!isRecord(raw) || typeof raw.chat_id !== "string") return null;
  return {
    chat_id: raw.chat_id,
    project: str(raw.project),
    title: str(raw.title, "Conversation"),
    state: oneOf(raw.state, CHAT_STATES, "idle"),
    updated_at: str(raw.updated_at),
  };
}

export function toChatEvent(raw: unknown): ChatEvent | null {
  if (!isRecord(raw) || typeof raw.chat_id !== "string") return null;
  if (!KINDS.includes(raw.kind as ChatEvent["kind"])) return null;
  const event: ChatEvent = { chat_id: raw.chat_id, kind: raw.kind as ChatEvent["kind"], at: str(raw.at) };
  const cost = num(raw.cost_usd);
  return {
    ...event,
    ...(typeof raw.text === "string" ? { text: raw.text } : {}),
    ...(typeof raw.tool === "string" ? { tool: raw.tool } : {}),
    ...(cost !== null ? { cost_usd: cost } : {}),
    ...(CHAT_STATES.includes(raw.state as ChatState) ? { state: raw.state as ChatState } : {}),
  };
}

// ---- commands ---------------------------------------------------------------

export async function getOsd(): Promise<Osd | null> {
  return toOsd(await invoke<unknown>("get_osd"));
}

export async function getConfig(): Promise<ConfigView | null> {
  return toConfigView(await invoke<unknown>("get_config"));
}

export async function setConfig(patch: ConfigPatch): Promise<ConfigView | null> {
  return toConfigView(await invoke<unknown>("set_config", { patch }));
}

export function showContextMenu(): Promise<void> {
  return invoke("show_context_menu");
}

export async function listProjects(): Promise<Project[]> {
  const res = await invoke<unknown>("list_projects");
  const raw = isRecord(res) ? res.projects : res;
  return list(raw)
    .filter(isRecord)
    .filter((p) => typeof p.name === "string" && p.name !== "")
    .map((p) => ({ name: String(p.name), path_label: str(p.path_label) }));
}

export async function chatCreate(project: string, text: string): Promise<string> {
  const res = await invoke<unknown>("chat_create", { project, text });
  if (!isRecord(res) || typeof res.chat_id !== "string") throw new Error("Réponse du démon invalide.");
  return res.chat_id;
}

export function chatSend(chatId: string, text: string): Promise<unknown> {
  return invoke("chat_send", { chatId, text });
}

export function chatStop(chatId: string): Promise<unknown> {
  return invoke("chat_stop", { chatId });
}

export async function chatList(): Promise<Chat[]> {
  const res = await invoke<unknown>("chat_list");
  const raw = isRecord(res) ? res.chats : res;
  return list(raw)
    .map(toChat)
    .filter((c): c is Chat => c !== null);
}

export async function chatGet(chatId: string): Promise<{ chat: Chat | null; messages: ChatMessage[] }> {
  const res = await invoke<unknown>("chat_get", { chatId });
  if (!isRecord(res)) return { chat: null, messages: [] };
  return { chat: toChat(res.chat), messages: toMessages(res.messages) };
}

export async function sessionTranscript(id: string): Promise<Transcript> {
  const res = await invoke<unknown>("session_transcript", { id });
  if (!isRecord(res)) return { messages: [], can_prompt: false };
  return { messages: toMessages(res.messages), can_prompt: res.can_prompt === true };
}
