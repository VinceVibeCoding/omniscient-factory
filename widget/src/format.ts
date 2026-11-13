// French labels and small formatting helpers.
import type { SessionState, Source, StepKind } from "./types";

export const STATE_LABEL: Record<SessionState, string> = {
  working: "en cours",
  waiting: "en attente",
  idle: "au repos",
  done: "terminé",
  error: "erreur",
};

export const STATE_GLYPH: Record<SessionState, string> = {
  working: "●",
  waiting: "◆",
  idle: "○",
  done: "✓",
  error: "✕",
};

export const SOURCE_LABEL: Record<Source, string> = {
  claude: "Claude Code",
  copilot: "GitHub Copilot",
  herdr: "herdr",
  agentrun: "AgentRun",
};

export const SOURCE_MARK: Record<Source, string> = {
  claude: "C",
  copilot: "G",
  herdr: "H",
  agentrun: "A",
};

export const STEP_LABEL: Record<StepKind, string> = {
  prompt: "Demande",
  tool: "Outil",
  tool_result: "Résultat",
  message: "Message",
  permission: "Permission",
  question: "Question",
  notice: "Avis",
  stop: "Fin",
  error: "Erreur",
};

export function stateLabel(state: string): string {
  return STATE_LABEL[state as SessionState] ?? state;
}

export function sourceLabel(source: string): string {
  return SOURCE_LABEL[source as Source] ?? source;
}

export function sourceMark(source: string): string {
  return SOURCE_MARK[source as Source] ?? "?";
}

export function stepLabel(kind: string): string {
  return STEP_LABEL[kind as StepKind] ?? kind;
}

/** Last two path segments: "/a/b/projects/app" → "projects/app". */
export function cwdTail(cwd: string | null): string {
  if (!cwd) return "";
  const parts = cwd.split(/[\\/]+/).filter(Boolean);
  return parts.slice(-2).join("/");
}

const MINUTE = 60;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

/** Relative age in French: "à l'instant", "3 min", "2 h", "4 j". */
export function age(iso: string, now: number = Date.now()): string {
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return "";
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 45) return "à l'instant";
  if (seconds < HOUR) return `${Math.round(seconds / MINUTE)} min`;
  if (seconds < DAY) return `${Math.round(seconds / HOUR)} h`;
  return `${Math.round(seconds / DAY)} j`;
}

/** Seconds until `iso`, or null when there is no deadline. */
export function secondsLeft(iso: string | null, now: number = Date.now()): number | null {
  if (!iso) return null;
  const at = Date.parse(iso);
  if (Number.isNaN(at)) return null;
  return Math.max(0, Math.round((at - now) / 1000));
}

export function clip(text: string, max: number): string {
  return text.length <= max ? text : `${text.slice(0, max - 1)}…`;
}

export function preview(value: unknown, max = 1200): string {
  if (typeof value === "string") return clip(value, max);
  try {
    return clip(JSON.stringify(value, null, 2) ?? "", max);
  } catch {
    return "(aperçu indisponible)";
  }
}

const FR = "fr-FR";

/** Compact token count: 980, 84 k, 1,2 M. */
export function compactTokens(n: number): string {
  if (!Number.isFinite(n) || n < 0) return "–";
  if (n < 1000) return String(Math.round(n));
  if (n < 1_000_000) return `${new Intl.NumberFormat(FR, { maximumFractionDigits: n < 10_000 ? 1 : 0 }).format(n / 1000)} k`;
  return `${new Intl.NumberFormat(FR, { maximumFractionDigits: 1 }).format(n / 1_000_000)} M`;
}

/** "0,84 $" */
export function dollars(n: number): string {
  return `${new Intl.NumberFormat(FR, { minimumFractionDigits: 2, maximumFractionDigits: 2 }).format(n)} $`;
}

/** Local "14:00" for an ISO timestamp, or "" when invalid. */
export function clock(iso: string | null): string {
  if (!iso) return "";
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return "";
  return new Intl.DateTimeFormat(FR, { hour: "2-digit", minute: "2-digit" }).format(at);
}

export type Level = "ok" | "warn" | "high";

/** < 60 % ok (signal), 60–85 % warn (ember), > 85 % high (rose). */
export function level(pct: number): Level {
  if (pct > 85) return "high";
  if (pct >= 60) return "warn";
  return "ok";
}
