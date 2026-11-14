// Session counts shown on the OSD (the pill itself was replaced by views/osd.ts).
import type { Session } from "../types";

export interface Counts {
  working: number;
  waiting: number;
  done: number;
}

export function countSessions(sessions: Session[]): Counts {
  return sessions.reduce(
    (acc, s) => ({
      working: acc.working + (s.state === "working" ? 1 : 0),
      waiting: acc.waiting + (s.state === "waiting" || s.pending.length > 0 ? 1 : 0),
      done: acc.done + (s.state === "done" ? 1 : 0),
    }),
    { working: 0, waiting: 0, done: 0 },
  );
}
