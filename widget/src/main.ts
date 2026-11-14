// Omnigroup widget: state, rendering and wiring to the Rust side.
import { listen } from "@tauri-apps/api/event";
import * as api from "./api";
import * as osdApi from "./api-osd";
import { ChatController } from "./chat-controller";
import { replace } from "./dom";
import { installDrag } from "./drag";
import { keepUiState, restoreUiState } from "./keep";
import { applySkin, applyTheme } from "./theme";
import type { ConfigPatch, ConfigView, LinkStatus, Osd, Pending, Session, SessionDetail, Settings, Step, StreamEvent, Transcript, Usage, WidgetConfig } from "./types";
import { chatView } from "./views/chat";
import { detailView } from "./views/detail";
import { displaySection } from "./views/display";
import { header, listView } from "./views/list";
import { osdView } from "./views/osd";
import { settingsView } from "./views/settings";
import { type Tab, tabBar, withTabs } from "./views/tabs";
import { transcriptSection } from "./views/transcript";

type View = "osd" | "list" | "detail" | "settings" | "chat";

interface AppState {
  readonly view: View;
  readonly sessions: readonly Session[];
  readonly detail: SessionDetail | null;
  readonly transcript: Transcript | null;
  readonly settings: Settings | null;
  readonly link: LinkStatus;
  readonly osd: Osd | null;
  readonly config: ConfigView | null;
}

const REFRESH_DEBOUNCE_MS = 250;
const AGE_TICK_MS = 30_000;
const TOAST_MS = 4_000;
const DEFAULT_CONFIG: WidgetConfig = { theme: "default", osd_view: "bars", show_ticker: true };

const app = document.getElementById("app") as HTMLElement;
const toastEl = document.getElementById("toast") as HTMLElement;

let state: AppState = {
  view: "osd",
  sessions: [],
  detail: null,
  transcript: null,
  settings: null,
  link: { state: "connecting", message: "Connexion au démon…" },
  osd: null,
  config: null,
};
let deferredRender = false;
let refreshTimer: number | undefined;
let transcriptTimer: number | undefined;
let toastTimer: number | undefined;
let sessionDraft = "";

const chat = new ChatController(
  () => state.view === "chat" && render(),
  (err) => toast(api.errorText(err)),
);

function update(patch: Partial<AppState>): void {
  state = { ...state, ...patch };
  render();
}

function toast(message: string): void {
  toastEl.textContent = message;
  toastEl.hidden = false;
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (toastEl.hidden = true), TOAST_MS);
}

function editing(): boolean {
  const active = document.activeElement;
  return active instanceof HTMLElement && app.contains(active) && active.closest("form") !== null;
}

function render(): void {
  if (editing()) {
    deferredRender = true;
    return;
  }
  deferredRender = false;
  document.body.dataset.view = state.view;
  const kept = keepUiState(app);
  replace(app, viewFor(state));
  restoreUiState(app, kept);
}

function selectTab(tab: Tab): void {
  if (tab === "sessions") update({ view: "list", detail: null, transcript: null });
  else if (tab === "settings") void openSettings();
  else {
    update({ view: "chat" });
    void chat.load();
  }
}

function viewFor(s: AppState): HTMLElement {
  const sessions = [...s.sessions];
  const nav = { open: openSession, collapse, settings: () => selectTab("settings") };
  switch (s.view) {
    case "osd":
      return osdView({ osd: s.osd, config: s.config?.config ?? DEFAULT_CONFIG, link: s.link, sessions }, () => void expand());
    case "detail":
      if (s.detail) return detailView(s.detail, detailHandlers, conversationFor(s.detail, s.transcript));
      return withTabs(listView(sessions, s.link, nav), tabBar("sessions", selectTab));
    case "settings":
      return settingsPanel(s);
    case "chat":
      return withTabs(chatView(chat.view, chat.handlers, header(s.link, nav, "Chat")), tabBar("chat", selectTab));
    default:
      return withTabs(listView(sessions, s.link, nav), tabBar("sessions", selectTab));
  }
}

function settingsPanel(s: AppState): HTMLElement {
  const panel = settingsView(s.settings ?? { daemon_url: "http://127.0.0.1:7341" }, s.link, settingsHandlers);
  panel.querySelector(".panel__body")?.prepend(displaySection(s.config, changeConfig));
  return withTabs(panel, tabBar("settings", selectTab));
}

function conversationFor(detail: SessionDetail, transcript: Transcript | null): HTMLElement {
  return transcriptSection(detail.id, transcript, sessionDraft, {
    setDraft: (text) => (sessionDraft = text),
    send: (text) => detailHandlers.prompt(detail.id, text),
  });
}

async function guarded<T>(work: () => Promise<T>): Promise<T | undefined> {
  try {
    return await work();
  } catch (err) {
    toast(api.errorText(err));
    return undefined;
  }
}

async function refreshSessions(): Promise<void> {
  const sessions = await guarded(api.listSessions);
  if (sessions) update({ sessions });
  if (state.view === "detail" && state.detail) {
    const detail = await guarded(() => api.getSession(state.detail?.id ?? ""));
    if (detail) update({ detail });
  }
}

/** Mode B: an older daemon (or a non-Claude session) has no transcript → null, no toast. */
async function loadTranscript(id: string): Promise<void> {
  let transcript: Transcript | null;
  try {
    transcript = await osdApi.sessionTranscript(id);
  } catch {
    transcript = null;
  }
  if (state.detail?.id === id) update({ transcript });
}

function scheduleRefresh(): void {
  window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => void refreshSessions(), REFRESH_DEBOUNCE_MS);
}

function scheduleTranscript(id: string): void {
  window.clearTimeout(transcriptTimer);
  transcriptTimer = window.setTimeout(() => void loadTranscript(id), REFRESH_DEBOUNCE_MS);
}

async function expand(): Promise<void> {
  await guarded(() => api.setWindowMode("panel"));
  if (state.link.state === "connected") update({ view: "list" });
  else await openSettings();
  app.querySelector<HTMLElement>("[data-row], [role=tab], button")?.focus();
}

function collapse(): void {
  update({ view: "osd", detail: null, transcript: null });
  void guarded(() => api.setWindowMode("osd"));
}

async function openSession(id: string): Promise<void> {
  const detail = await guarded(() => api.getSession(id));
  if (!detail) return;
  sessionDraft = "";
  update({ view: "detail", detail, transcript: null });
  void loadTranscript(id);
  app.querySelector<HTMLElement>(".card button, .card input, .actions button")?.focus();
}

async function openSettings(): Promise<void> {
  const settings = await guarded(api.getSettings);
  update({ view: "settings", settings: settings ?? state.settings });
}

function applyConfig(view: ConfigView | null): void {
  if (!view) return;
  applySkin(view.config.theme);
  update({ config: view });
}

function changeConfig(patch: ConfigPatch): void {
  void guarded(async () => applyConfig(await osdApi.setConfig(patch)));
}

const detailHandlers = {
  back: () => update({ view: "list", detail: null, transcript: null }),
  answer: (pending: Pending, answer: api.Answer) =>
    void guarded(async () => {
      await api.answerPending(pending.id, answer);
      toast("Réponse envoyée.");
      await refreshSessions();
    }),
  jump: (id: string) =>
    void guarded(async () => {
      const res = await api.jumpSession(id);
      toast(res.message);
    }),
  dismiss: (id: string) =>
    void guarded(async () => {
      await api.dismissSession(id);
      update({ view: "list", detail: null, transcript: null, sessions: state.sessions.filter((s) => s.id !== id) });
    }),
  prompt: (id: string, text: string) =>
    void guarded(async () => {
      const res = await api.promptSession(id, text);
      toast(res.message);
      sessionDraft = "";
      (document.activeElement as HTMLElement | null)?.blur();
      await refreshSessions();
      scheduleTranscript(id);
    }),
};

const settingsHandlers = {
  back: () => update({ view: "list" }),
  save: (settings: Settings) =>
    void guarded(async () => {
      const saved = await api.saveSettings(settings);
      toast("Réglages enregistrés.");
      (document.activeElement as HTMLElement | null)?.blur();
      update({ settings: saved, view: "list" });
    }),
  pairStart: () =>
    void guarded(async () => {
      toast(await api.pairStart());
    }),
  pairFinish: (code: string) =>
    void guarded(async () => {
      if (!/^[0-9]{6}$/.test(code)) {
        toast("Le code de couplage fait 6 chiffres.");
        return;
      }
      toast(await api.pairFinish(code));
      (document.activeElement as HTMLElement | null)?.blur();
      update({ view: "list" });
    }),
  unpair: () =>
    void guarded(async () => {
      toast(await api.unpair());
    }),
};

function withUsage<T extends Session>(session: T, id: string, usage: Usage | null): T {
  return session.id === id ? { ...session, usage } : session;
}

function onUsageEvent(data: unknown): void {
  if (typeof data !== "object" || data === null) return;
  const { session_id: id, usage } = data as { session_id?: unknown; usage?: unknown };
  if (typeof id !== "string") return;
  const value = typeof usage === "object" && usage !== null ? (usage as Usage) : null;
  update({
    sessions: state.sessions.map((s) => withUsage(s, id, value)),
    detail: state.detail ? withUsage(state.detail, id, value) : null,
  });
}

function onOsd(data: unknown): void {
  const osd = osdApi.toOsd(data);
  if (!osd) return;
  if (state.view === "osd") update({ osd });
  else state = { ...state, osd }; // no re-render of an open panel every 2 s
}

function onStreamEvent(event: StreamEvent): void {
  switch (event.type) {
    case "osd":
      return onOsd(event.data);
    case "config":
      return applyConfig(osdApi.toConfigView(event.data));
    case "chat": {
      const chatEvent = osdApi.toChatEvent(event.data);
      if (chatEvent) chat.onEvent(chatEvent);
      return;
    }
    case "usage":
      return onUsageEvent(event.data);
    case "menu_error": {
      const data = event.data as { message?: unknown } | null;
      if (data && typeof data.message === "string") toast(data.message);
      return;
    }
  }
  if (event.type === "step" && state.detail) {
    const step = event.data as Step;
    if (step && step.session_id === state.detail.id) {
      update({ detail: { ...state.detail, steps: [...state.detail.steps, step] } });
      scheduleTranscript(state.detail.id);
    }
  }
  scheduleRefresh();
}

async function onLink(link: LinkStatus): Promise<void> {
  update({ link });
  if (link.state !== "connected") return;
  const theme = await guarded(api.getTheme);
  if (theme !== undefined) applyTheme(theme);
  applyConfig((await guarded(osdApi.getConfig)) ?? null);
  const osd = await guarded(osdApi.getOsd);
  if (osd) update({ osd });
  await refreshSessions();
}

function onKey(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  if (state.view === "chat" && chat.view.activeId) chat.handlers.close();
  else if (state.view === "detail" || state.view === "settings" || state.view === "chat") update({ view: "list", detail: null, transcript: null });
  else if (state.view === "list") collapse();
}

/** Right-click on the OSD opens the native menu (OSD View ▸, Thème ▸, ticker…). */
function onContextMenu(event: MouseEvent): void {
  const target = event.target as Element | null;
  if (state.view !== "osd" && target?.closest("input, textarea, select")) return; // keep copy/paste
  event.preventDefault();
  if (state.view === "osd") void guarded(osdApi.showContextMenu);
}

async function main(): Promise<void> {
  applySkin("default");
  render();
  document.addEventListener("keydown", onKey);
  document.addEventListener("contextmenu", onContextMenu);
  installDrag(document, api.startDrag);
  app.addEventListener("focusout", () => window.setTimeout(() => deferredRender && render(), 0));
  window.setInterval(() => state.view !== "detail" && state.view !== "chat" && render(), AGE_TICK_MS);
  await listen<StreamEvent>("group://event", (e) => onStreamEvent(e.payload));
  await listen<LinkStatus>("group://status", (e) => void onLink(e.payload));
  await listen<string>("group://open", (e) => (e.payload ? void openSession(e.payload) : void expand()));
  const settings = await guarded(api.getSettings);
  if (settings) state = { ...state, settings };
  const link = await guarded(api.linkStatus);
  if (link) await onLink(link);
}

void main();
