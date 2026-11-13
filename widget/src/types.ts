// Shapes of the omnigroup daemon's JSON (see src/omniscient/group/models.py).

export type Source = "claude" | "copilot" | "herdr" | "agentrun";
export type SessionState = "working" | "waiting" | "idle" | "done" | "error";
export type StepKind =
  | "prompt"
  | "tool"
  | "tool_result"
  | "message"
  | "permission"
  | "question"
  | "notice"
  | "stop"
  | "error";
export type PendingKind = "permission" | "question" | "budget" | "approval_external";
export type PendingStatus = "open" | "answered" | "expired" | "cancelled";

export interface JumpTarget {
  kind: "vscode_session" | "vscode_folder" | "herdr" | "url" | "terminal";
  value: string;
  label: string;
}

export interface Pending {
  id: string;
  session_id: string;
  kind: PendingKind;
  created_at: string;
  expires_at: string | null;
  tool: string | null;
  payload: Record<string, unknown>;
  options: string[];
  status: PendingStatus;
  answer: Record<string, unknown> | null;
}

export interface Usage {
  model: string | null;
  input_tokens: number;
  output_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  context_tokens: number | null;
  context_limit: number | null;
  cost_usd: number | null;
  plan_used_pct: number | null;
  plan_resets_at: string | null;
  updated_at: string;
}

export interface UsageTotals {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number | null;
}

export interface Session {
  id: string;
  source: Source;
  native_id: string;
  title: string;
  cwd: string | null;
  state: SessionState;
  started_at: string;
  updated_at: string;
  last_step: string;
  dismissed: boolean;
  jump: JumpTarget | null;
  meta: Record<string, string>;
  pending: Pending[];
  usage?: Usage | null;
}

export interface Step {
  session_id: string;
  at: string;
  kind: StepKind;
  summary: string;
  detail: string | null;
}

export interface SessionDetail extends Session {
  steps: Step[];
}

/** Only the daemon URL: the widget token stays in the Windows vault (never here). */
export interface Settings {
  daemon_url: string;
}

export type LinkState = "connecting" | "connected" | "unpaired" | "unauthorized" | "offline";

export interface LinkStatus {
  state: LinkState;
  message: string;
}

export interface StreamEvent {
  type: "session" | "step" | "pending" | "resolved" | string;
  data: unknown;
}

export interface ActionResult {
  ok: boolean;
  message: string;
}
