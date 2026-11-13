# Contributing

Thanks for helping. Omniscient is small and opinionated; these rules keep it that way.

## Setup

```bash
git clone https://github.com/VinceVibeCoding/omniscient-factory
cd omniscient-factory
uv sync
uv run pytest -q
```

To try your changes against a throwaway home instead of your real one:

```bash
export OMNISCIENT_HOME=/tmp/omniscient-dev
uv run omniscient init
```

## Before you open a pull request

```bash
uv run ruff check src tests
uv run ruff format src tests
uv run pytest -q
```

CI runs the same three steps on Ubuntu with Python 3.12.

## Code rules

- **Tests first** where you can (red, green, refactor). Aim for 80%+ coverage on new code.
- **Tests are offline.** No network, no real model, Slack, herdr, GitHub or AgentRun calls:
  inject fakes (most modules take a client, `fetch`, `run` or `ask` parameter for this).
- **Immutable data:** `@dataclass(frozen=True)`; return new values instead of mutating.
- **Small units:** files under 400 lines, functions under 50 lines.
- **Validate at boundaries** (CLI arguments, files, agent output, HTTP responses) and fail
  with a clear message. Never swallow an exception.
- **No hardcoded paths.** Resolve data locations through `omniscient.home.find_home()`,
  lazily at command time, so tests can point `OMNISCIENT_HOME` at a temp folder.
- **No personal data** in code, tests or fixtures: no real names, user paths, e-mails,
  Slack/GitHub ids or tokens. Use generic names (`run-tracker`, `card-index`) and fake ids
  (`C0TEST`, `U0TEST`).
- Model spending stays capped in code (budget checks), never only in prompts.

## Language

User-facing text is French today. English is welcome; please route new strings through
`src/omniscient/i18n.py` rather than adding a second hardcoded language. Code, comments,
commit messages and docs are in English.

## Commits and pull requests

Conventional commits (`feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`).
Keep pull requests focused and fill in the template. For a larger change, open an
issue first so we can agree on the smallest version.

## Agents working on this repo

Coding agents should read [AGENTS.md](AGENTS.md) first.
