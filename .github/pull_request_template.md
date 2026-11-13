## What and why

<!-- One or two sentences. Link the issue if there is one. -->

## How it was tested

- [ ] `uv run pytest -q` passes (tests stay offline: no network, no real model, Slack, herdr or AgentRun calls)
- [ ] `uv run ruff check src tests` and `uv run ruff format --check src tests` pass
- [ ] New behavior has tests (written first when possible)

## Checklist

- [ ] Frozen dataclasses, files under 400 lines, input validated at boundaries, no swallowed errors
- [ ] No secrets, personal paths, user names or real Slack/GitHub ids in code, tests or fixtures
- [ ] User-facing text: French today; English welcome if routed through the i18n layer
