// Thin typed wrappers over the Rust commands (all networking lives in Rust).
import { invoke } from "@tauri-apps/api/core";
import type { ActionResult, LinkStatus, Session, SessionDetail, Settings, UsageTotals } from "./types";

export type Answer =
  | { decision: "allow" | "deny" | "always"; message?: string }
  | { answers: Record<string, string> }
  | { choice: "raise" | "stop" };

export type WindowMode = "pill" | "panel";

export function errorText(err: unknown): string {
  if (typeof err === "string") return err;
  if (err instanceof Error) return err.message;
  return "Erreur inconnue.";
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function normalizeSession<T extends Session>(s: T): T {
  return { ...s, pending: Array.isArray(s.pending) ? s.pending : [], usage: isRecord(s.usage) ? s.usage : null };
}

export async function listSessions(): Promise<Session[]> {
  const res = await invoke<{ sessions?: Session[] }>("list_sessions");
  return Array.isArray(res.sessions) ? res.sessions.map(normalizeSession) : [];
}

/** Accepts {session, steps, pending} or a flat session with steps/pending. */
export async function getSession(id: string): Promise<SessionDetail> {
  const res = await invoke<Record<string, unknown>>("get_session", { id });
  const base = isRecord(res.session) ? res.session : res;
  const steps = Array.isArray(res.steps) ? res.steps : Array.isArray(base.steps) ? base.steps : [];
  const pending = Array.isArray(res.pending) ? res.pending : base.pending;
  return normalizeSession({ ...(base as unknown as SessionDetail), steps, pending } as SessionDetail);
}

/** GET /usage totals; null when the daemon doesn't expose them. */
export async function getUsage(): Promise<UsageTotals | null> {
  const res = await invoke<unknown>("get_usage");
  if (!isRecord(res)) return null;
  const totals = isRecord(res.totals) ? res.totals : res;
  if (typeof totals.input_tokens !== "number" || typeof totals.output_tokens !== "number") return null;
  return {
    input_tokens: totals.input_tokens,
    output_tokens: totals.output_tokens,
    cost_usd: typeof totals.cost_usd === "number" ? totals.cost_usd : null,
  };
}

export function answerPending(id: string, answer: Answer): Promise<unknown> {
  return invoke("answer_pending", { id, answer });
}

export function dismissSession(id: string): Promise<unknown> {
  return invoke("dismiss_session", { id });
}

export function jumpSession(id: string): Promise<ActionResult> {
  return invoke<ActionResult>("jump_session", { id });
}

export function promptSession(id: string, text: string): Promise<ActionResult> {
  return invoke<ActionResult>("prompt_session", { id, text });
}

export function getTheme(): Promise<unknown> {
  return invoke("get_theme");
}

export function getSettings(): Promise<Settings> {
  return invoke<Settings>("get_settings");
}

export function saveSettings(settings: Settings): Promise<Settings> {
  return invoke<Settings>("save_settings", { settings });
}

export function pairViaWsl(): Promise<Settings> {
  return invoke<Settings>("pair_via_wsl");
}

export function setWindowMode(mode: WindowMode): Promise<void> {
  return invoke("set_window_mode", { mode });
}

export function linkStatus(): Promise<LinkStatus> {
  return invoke<LinkStatus>("link_status");
}
