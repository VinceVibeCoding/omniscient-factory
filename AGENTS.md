# Omniscient (engine)

Omniscient is an autonomous idea factory: it reads tech and AI news, invents ideas about
what can now be built, has a council of agents debate and score them, plans the best ones
and builds throwaway proofs of concept. This repository is the **engine**. Each user's
data lives in a separate **home** folder (see `src/omniscient/home.py`).

## The five loops

| Loop | What it does | Code | Writes to (in the home) |
|---|---|---|---|
| **Sense** | news notes from GitHub, Hacker News and AI vendor blogs | `src/omniscient/sense/` | `vault/news/` |
| **Think** | visionary ideas → council debate → scores | `src/omniscient/think/` | `vault/ideas/` |
| **Plan** | strategist: game plan, WIP limit, wayfinder maps | `src/omniscient/plan/` | `vault/gameplan.md`, `vault/maps/` |
| **Build** | builder agent in an AgentRun Docker box | `src/omniscient/build/` | `pocs/<idea>/` |
| **Learn** | agents' kept notes → tech knowledge base | `src/omniscient/learn/` | `vault/tech/learned/` |

Routing (task → model + effort) is in `src/omniscient/router.py`. The studio (herdr board
and ateliers) is in `src/omniscient/studio/`; onboarding (`omniscient init`) in
`src/omniscient/onboarding/`. Agent definitions and studio skills ship in
`src/omniscient/resources/`.

## Rules for every agent

1. **Read before you build.** Check the home's `vault/tech/` notes and the live vendor docs
   before writing code. When you find a new caveat, write it back to `vault/tech/`.
2. **Know the blast radius.** Before touching a registered project, read its
   `repos/<name>.md` entry and that project's own AGENTS.md or CLAUDE.md.
3. **Autonomous zone:** sensing, ideation, the game plan, and prototypes under `pocs/`.
4. **Approval required:** any change to a registered project (worktree branch + pull
   request, never its main branch), spending over the daily budget, and anything public or
   outward-facing (publishing, cloud resources that cost money).
5. **Read other projects, never write to them directly.** Writes go only through worktrees.
6. **Prototypes are throwaway; the engine is not.** A prototype answers one question and
   starts with one command. Engine code follows rule 7.
7. **Code standards:** frozen dataclasses, files under 400 lines, functions under 50 lines,
   validation at every boundary, no swallowed errors, TDD with 80%+ coverage, offline tests.
8. **Never answer a human's side of a HITL ticket.** Grilling and prototype verdicts come
   only from the owner. The council may debate among itself, but no agent stands in for
   the owner.
9. **No personal data** in the engine: no user names, absolute home paths, e-mails, Slack
   or GitHub ids, tokens. Paths come from `find_home()`.

## Idea notes

Notes change only through `omniscient idea list|show|context|new|variant|append|debate|verdict`
(`src/omniscient/studio/ideas.py` is the contract). A studio council verdict counts like
the factory's: `abandonner` drops the idea; a debated idea at or above the promote
threshold can be planned by the next cycle.

## Issue tracker

Local markdown in the home's vault, under `vault/maps/<feature>/`. See
`docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md` and `docs/agents/domain.md`.

## Commands

```
uv run pytest -q                 # tests (offline)
uv run ruff check src tests      # lint
uv run omniscient init           # create or update a home
uv run omniscient studio         # idea board (inside herdr)
uv run omniscient agents-push    # sync agent definitions to AgentRun
uv run omniscient cycle          # ingest -> learn-sync -> think -> plan -> slack-digest
uv run omniscient build STEM     # build one idea's prototype
```
