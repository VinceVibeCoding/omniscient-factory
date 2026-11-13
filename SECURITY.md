# Security policy

## Reporting a vulnerability

Please do **not** open a public issue for security problems. Use GitHub's
[private vulnerability reporting](https://github.com/VinceVibeCoding/omniscient-factory/security/advisories/new)
on this repository. You should get a first answer within a week.

Include what you found, how to reproduce it, and the impact you expect. Remove any real
API key or token from the report.

## Supported versions

Only the latest commit on `main` is supported. There are no release branches yet.

## How omniscient handles secrets and risk

- Secrets live in `<home>/.env` (created by `omniscient init` with mode 600). They are never
  echoed, and the engine never writes them into notes, prompts or prototypes.
- Prototypes are built by the `builder` agent. What Omniscient itself configures
  (`src/omniscient/resources/agents/builder.json`, `src/omniscient/build/poc.py`): the run is
  placed in an AgentRun **Docker sandbox box** (`place: {mode: sandbox, box: docker}`), with
  only shell and file tools (`bash`, `read`, `write`, `edit`; no web search or fetch tool),
  a per-run cap of $1.50, and a prompt built from the idea note and its source news
  items (no `.env`, no API keys). Only the run's `work/` folder is copied back, into `pocs/<idea>/`. Network egress
  of the box is **not** configured by Omniscient: it is whatever your AgentRun Docker
  sandbox allows. The builder is *instructed* to use package registries only and to fake
  external APIs with sample data, but that is a prompt, not an enforcement; restrict the
  box's network in AgentRun if you need a guarantee.
- Before a prototype's web demo is published, it is scanned for secret-looking strings and
  local user paths, and publishing is refused on any hit.
- Model spending is capped in code (`[budget]` in `config/omniscient.toml`), not by prompts.
- **Untrusted text reaches agent prompts (prompt injection).** News items, idea notes and
  the registry entries of the projects you register are pasted into agent prompts (for
  example the visionary, council and builder). A registry entry's purpose line comes from
  the project's README or manifest, local or on GitHub (including public repos of other
  users), so a crafted README or article can try to steer those agents. Mitigations in place: `omniscient init` / `project add` read at
  most 64 KB per file, never follow symlinks or files outside the project folder, never
  read `.env`, and mask secret-looking strings (tokens, keys, local paths) before writing
  a registry entry; agents get no API keys; spending is capped in code. Register only
  projects you trust, and review an agent's output before acting on it.
- Agents only read your other projects. Any change to them is meant to go through a
  worktree branch and a pull request you approve.

## Scope

In scope: this repository's code (`src/`, `scripts/`, CI). Out of scope: AgentRun, herdr,
Claude Code, Slack and other third-party tools it drives; report those upstream.
