// Omnigroup widget: state, rendering and wiring to the Rust side.
import { listen } from "@tauri-apps/api/event";
import * as api from "./api";
import { replace } from "./dom";
import { installDrag } from "./drag";
import { applyTheme } from "./theme";
import type {
  LinkStatus,
  Pending,
  Session,
  SessionDetail,
  Settings,
  Step,
  StreamEvent,
  Usage,
  UsageTotals,
} from "./types";
import { detailView } from "./views/detail";
import { listView } from "./views/list";
import { pillView } from "./views/pill";
import { settingsView } from "./views/settings";

type View = "pill" | "list" | "detail" | "settings";

interface AppState {
  readonly view: View;
  readonly sessions: readonly Session[];
  readonly detail: SessionDetail | null;
  readonly settings: Settings | null;
  readonly link: LinkStatus;
  readonly totals: UsageTotals | null;
}

const REFRESH_DEBOUNCE_MS = 250;
const AGE_TICK_MS = 30_000;
const TOAST_MS = 4_000;

const app = document.getElementById("app") as HTMLElement;
const toastEl = document.getElementById("toast") as HTMLElement;

let state: AppState = {
  view: "pill",
  sessions: [],
  detail: null,
  settings: null,
  link: { state: "connecting", message: "Connexion au démon…" },
  totals: null,
};
let deferredRender = false;
let refreshTimer: number | undefined;
let totalsTimer: number | undefined;
let toastTimer: number | undefined;

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
  replace(app, viewFor(state));
}

function viewFor(s: AppState): HTMLElement {
  const sessions = [...s.sessions];
  const nav = { open: openSession, collapse, settings: openSettings };
  switch (s.view) {
    case "pill":
      return pillView(sessions, s.link, s.totals, () => void expand());
    case "detail":
      return s.detail ? detailView(s.detail, detailHandlers) : listView(sessions, s.link, nav);
    case "settings":
      return settingsView(s.settings ?? emptySettings(), s.link, settingsHandlers);
    default:
      return listView(sessions, s.link, nav);
  }
}

function emptySettings(): Settings {
  return { daemon_url: "http://127.0.0.1:7341" };
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
  await refreshTotals();
  if (state.view === "detail" && state.detail) {
    const detail = await guarded(() => api.getSession(state.detail?.id ?? ""));
    if (detail) update({ detail });
  }
}

/** GET /usage is optional: an older daemon without it just hides the pill total. */
async function refreshTotals(): Promise<void> {
  try {
    update({ totals: await api.getUsage() });
  } catch {
    update({ totals: null }); // endpoint missing or daemon down: the total is hidden, not an error
  }
}

function scheduleRefresh(): void {
  window.clearTimeout(refreshTimer);
  refreshTimer = window.setTimeout(() => void refreshSessions(), REFRESH_DEBOUNCE_MS);
}

async function expand(): Promise<void> {
  await guarded(() => api.setWindowMode("panel"));
  update({ view: "list" });
  app.querySelector<HTMLElement>("[data-row], button")?.focus();
}

function collapse(): void {
  update({ view: "pill", detail: null });
  void guarded(() => api.setWindowMode("pill"));
}

async function openSession(id: string): Promise<void> {
  const detail = await guarded(() => api.getSession(id));
  if (!detail) return;
  update({ view: "detail", detail });
  app.querySelector<HTMLElement>(".card button, .card input, .actions button")?.focus();
}

async function openSettings(): Promise<void> {
  const settings = await guarded(api.getSettings);
  update({ view: "settings", settings: settings ?? state.settings });
}

const detailHandlers = {
  back: () => update({ view: "list", detail: null }),
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
      update({ view: "list", detail: null, sessions: state.sessions.filter((s) => s.id !== id) });
    }),
  prompt: (id: string, text: string) =>
    void guarded(async () => {
      const res = await api.promptSession(id, text);
      toast(res.message);
      (document.activeElement as HTMLElement | null)?.blur();
      await refreshSessions();
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

function onStreamEvent(event: StreamEvent): void {
  if (event.type === "usage") {
    onUsageEvent(event.data);
    window.clearTimeout(totalsTimer);
    totalsTimer = window.setTimeout(() => void refreshTotals(), REFRESH_DEBOUNCE_MS);
    return;
  }
  if (event.type === "step" && state.detail) {
    const step = event.data as Step;
    if (step && step.session_id === state.detail.id) {
      update({ detail: { ...state.detail, steps: [...state.detail.steps, step] } });
    }
  }
  scheduleRefresh();
}

async function onLink(link: LinkStatus): Promise<void> {
  update({ link });
  if (link.state !== "connected") return;
  const theme = await guarded(api.getTheme);
  if (theme !== undefined) applyTheme(theme);
  await refreshSessions();
}

function onKey(event: KeyboardEvent): void {
  if (event.key !== "Escape") return;
  if (state.view === "detail" || state.view === "settings") update({ view: "list", detail: null });
  else if (state.view === "list") collapse();
}

async function main(): Promise<void> {
  render();
  document.addEventListener("keydown", onKey);
  installDrag(document, api.startDrag);
  app.addEventListener("focusout", () => window.setTimeout(() => deferredRender && render(), 0));
  window.setInterval(() => state.view !== "detail" && render(), AGE_TICK_MS);
  await listen<StreamEvent>("group://event", (e) => onStreamEvent(e.payload));
  await listen<LinkStatus>("group://status", (e) => void onLink(e.payload));
  await listen<string>("group://open", (e) => (e.payload ? void openSession(e.payload) : void expand()));
  const settings = await guarded(api.getSettings);
  if (settings) state = { ...state, settings };
  const link = await guarded(api.linkStatus);
  if (link) await onLink(link);
}

void main();
