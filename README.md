# omniscient-factory

**An idea factory for builders.** Omniscient reads tech and AI news, turns it into ideas
about what you could build now, has a council of agents debate and score them, plans the
best ones, and builds throwaway proofs of concept in a sandbox. You steer it from a
terminal **studio** that runs inside **herdr** (a terminal workspace manager):
an idea board plus one "atelier" (workshop) per idea, where Claude Code helps you push the
idea further.

```
news ──▶ ideas ──▶ council (debate + score) ──▶ game plan ──▶ prototype (PoC)
 Sense     Think          Think                    Plan          Build
                         ▲
              studio: you riff, fork, remix and judge ideas
```

> **Language note.** The interface (CLI messages, studio, Slack texts) is currently in
> **French**. Agent-written content follows the language in your profile (`fr` or `en`).
> Contributions that make the UI translatable are very welcome (see `src/omniscient/i18n.py`).

## Requirements

| Tool | Needed for | Required? |
|---|---|---|
| [uv](https://docs.astral.sh/uv/) and Python 3.12+ | installing and running | yes |
| git | project registry, prototypes | yes |
| herdr | the studio (ateliers are herdr workspaces) | for `studio` |
| [Claude Code](https://claude.com/claude-code) | the default atelier agent | for ateliers |
| [AgentRun](https://pypi.org/project/parcha-agentrun/) server on `127.0.0.1:7340` + Docker | the factory loops (news, ideas, council, plan, builds) | for the factory |
| Anthropic API key | the factory loops (AgentRun calls the models) | for the factory |
| Slack app (Socket Mode) | daily digest, buttons, debate threads | optional |

`omniscient init` checks these for you and never installs anything. Linux and WSL2 are
the tested platforms; `scripts/install-docker-wsl.sh` and `scripts/agentrun-up.sh` help on WSL2.

## Install

```bash
uv tool install git+https://github.com/VinceVibeCoding/omniscient-factory
omniscient init
```

Contributors: clone and run from the checkout.

```bash
git clone https://github.com/VinceVibeCoding/omniscient-factory
cd omniscient-factory
uv sync
uv run omniscient --help
```

## `omniscient init`

An interactive, re-runnable setup (re-running updates without deleting your data):

1. prints a prerequisites table (uv, git, Docker, herdr, Claude Code, AgentRun);
2. creates your **home** folder (default `~/.omniscient`) from the packaged templates,
   asks for your projects folder (default `~/projects`) and copies the studio skills;
3. writes your **profile**: language (`fr`/`en`), name, interests, topics to avoid;
4. registers your projects: scans your projects folder and/or lists your public GitHub
   repos so you pick which ones the factory may read and propose upgrades for (an
   optional `GITHUB_TOKEN` is asked right before the GitHub listing, to avoid rate limits);
5. stores secrets in `<home>/.env` (mode 600, written atomically, never echoed):
   `ANTHROPIC_API_KEY` (skip it to use the studio only), `AGENTRUN_API_KEY` (see
   [AgentRun](#agentrun)), and optional Slack tokens. Run it in a real terminal: when
   stdin is not a TTY, `init` warns that typed secrets may be echoed;
6. prints the next steps.

Add a project later with `omniscient project add <path | owner/name | GitHub URL | GitHub user>`
and see them with `omniscient project list`. A bare `NAME` that is both a folder in the
current directory and a possible GitHub user is read as the folder only if it holds
`.git` or a manifest (`pyproject.toml`, `package.json`, …); otherwise it is the GitHub
user. Write `./NAME` to force the folder.

## Your first 5 minutes

```bash
omniscient init                    # home, profile, projects, keys
herdr                              # open herdr, then inside it:
omniscient studio                  # the idea board
```

On the board: `n` new idea (Claude Code sharpens your raw idea into a note, then the
council scores it), `Entrée` opens the idea's atelier, `v` variant, `space` + `m` remix
2-3 ideas, `p`/`x` pursue/abandon, `f` filter, `s` skills library, `w` war room.

An **atelier** is a herdr workspace split 2×2:

- **agent**: Claude Code on your own subscription, primed with `/omniscient-riff`,
  `/omniscient-variant`, `/omniscient-remix` or `/omniscient-seed`; `a` switches to
  another agent (e.g. an Omniscient agent through `omniscient chat`, budget-capped);
- **à faire** (to do): the idea's checklist and a compact view of the note;
- **UI**: build progress and a local live-reload server for the prototype; `b` builds
  only after a cost confirmation;
- **salle de crise** (war room): who did what on which idea, agent relays, costs.

When AgentRun and an API key are set up, run the factory once by hand:

```bash
omniscient agents-push             # send the agent definitions to AgentRun
omniscient cycle                   # ingest -> learn-sync -> think -> plan -> slack-digest
```

## AgentRun

The factory loops run their agents on a local [AgentRun](https://pypi.org/project/parcha-agentrun/)
server (`http://127.0.0.1:7340`, override with `AGENTRUN_BASE_URL`). Start it, then its
runner (on WSL2, `scripts/agentrun-up.sh server` and `scripts/agentrun-up.sh runner`).
AgentRun is what calls the models, so your Anthropic key must be available to the
AgentRun server (see AgentRun's docs). Omniscient itself only needs AgentRun's **API
key**, read in this order:

1. the `AGENTRUN_API_KEY` environment variable;
2. `AGENTRUN_API_KEY` in `<home>/.env` (asked by `omniscient init`; leave it empty to use 3);
3. `~/.agentrun/.token`, which the local AgentRun server writes on its first start.

If no key is found or the server does not answer, `omniscient cycle` stops before any
stage with one message saying what to fix, and `think`, `plan`, `agents-push`… exit with
the same message.

## Daily schedule (systemd)

`omniscient schedule install` prints systemd **user** units for a daily cycle (and
optional Slack bot and [Omnigroup](#omnigroup) daemon services), rendered with your home
folder and the absolute path of the `omniscient` executable you are running. Nothing is
written until you add `--write` (to `~/.config/systemd/user/`), and Omniscient never runs
`systemctl`: it prints the command for you to run.

```bash
omniscient schedule install            # preview the units
omniscient schedule install --write    # write them, then run what it prints:
systemctl --user daemon-reload && systemctl --user enable --now omniscient-cycle.timer
omniscient schedule status             # are the unit files written and up to date?
omniscient schedule uninstall --write  # after `systemctl --user disable --now …`
```

On WSL2, systemd must be enabled (`[boot] systemd=true` in `/etc/wsl.conf`).

## Omnigroup

**Omnigroup** is a small always-on-top Windows widget that shows every agent you have
running: Claude Code (VS Code and CLI), GitHub Copilot (through hooks), herdr panes and
Omniscient's AgentRun runs. Click a session to see its steps; approve a permission,
answer a question, raise a budget cap, jump back to the session or cross it off, without
opening VS Code. It is **widget first, with a fallback**: if you do not answer in time,
the agent shows its normal prompt.

```bash
omniscient group serve                       # the daemon (127.0.0.1:7341 only), Ctrl-C to stop
omniscient group hooks install --target all  # preview the hook changes (a diff)
omniscient group hooks install --target all --write   # backup, then write
omniscient group hooks install --target claude --statusline --write   # optional status line
omniscient group status                      # is the daemon up? sessions and open requests
omniscient group widget                      # where to get the Windows installer, pairing
```

- **Install the widget.** Download the NSIS installer from the
  [releases](https://github.com/VinceVibeCoding/omniscient-factory/releases) (`widget-v*`
  tags) or the `omnigroup-windows-nsis` artifact of the latest *Widget* workflow run.
- **Pairing.** The widget reads the daemon's token with `wsl.exe -e omniscient group token`;
  if that fails, paste the output of `omniscient group token` in its settings. To start the
  daemon with your session, `omniscient schedule install --write` also writes an optional
  `omnigroup.service` (`systemctl --user enable --now omnigroup.service`).
- **Hooks.** The installer writes the absolute path of the light `omniscient-hook` script
  (about 60 ms to start, against about 400 ms for the full CLI) into
  `~/.claude/settings.json` and `~/.copilot/hooks/omnigroup.json`, keeps every other
  hook, and backs the file up first. `uninstall` removes only its own entries.
- **Fallback.** A hook waits for your answer at most 100 s for Claude Code (hook timeout
  110 s) and 25 s for Copilot (`timeoutSec` 30), then exits without output. If the daemon
  is down or the token is missing, it exits at once, silently: the agent behaves as usual.
- **Privacy and what hooks can do.** The daemon listens on `127.0.0.1` only, every route
  but `/health` needs a bearer token (`<home>/.omniscient/group.token`, mode 600), and only
  the widget's origins are allowed. Hooks can allow or deny one tool call, add an allow
  rule **for the current session only** ("Toujours (cette session)"), and answer
  `AskUserQuestion`; nothing is ever written to your permission settings. State stays in a
  local SQLite file, with secrets redacted from step previews.
- **Not verified yet** (see [docs/omnigroup.md](docs/omnigroup.md)): the herdr send-keys
  mapping used to answer a pane's dialog, Copilot hook payloads and replies, the
  `AskUserQuestion` answers format, and the full `allow` round trip on a live Claude Code
  session (a hook that prints nothing was checked to fall back correctly).

## Themes

The studio is themeable: `omniscient theme list`, `omniscient theme set <name>` and
`omniscient theme preview <name>`. Palettes follow the popular Omarchy-style themes
(tokyo-night, catppuccin, catppuccin-latte, gruvbox, nord, everforest, kanagawa,
rose-pine, matte-black, osaka-jade, ristretto) plus `omniscient`, the house theme (deep
indigo, a warm gold eye, a cyan signal). WSL users can export the palette as a
Windows Terminal color scheme and as herdr colors so the whole terminal matches.

## Costs

Two kinds of spending, kept separate on purpose:

- **API credit (the factory).** News, ideation, council, planning, builds and
  `omniscient chat` run through AgentRun on your Anthropic API key. Each agent has a
  per-run cap (`max_cost_usd`) and the code enforces a daily cap and a per-cycle cap
  (`[budget] daily_usd`, `per_cycle_usd` in `config/omniscient.toml`; the template ships
  with $5/day and $2/cycle). A run that could cross the daily cap is refused, and
  `max_cycles_per_day` stops repeated cycles. Routine work runs on Haiku, coding on
  Sonnet, ideation and strategy on Opus.
- **Your Claude Code subscription (the ateliers).** The default atelier agent is Claude
  Code, so riffing in the studio uses your subscription, not the API budget.

The studio and the `omniscient idea …` commands work with no API key at all.

## Architecture

Five loops, each a plain Python module driving AgentRun agents:

| Loop | What it does | Code | Writes to |
|---|---|---|---|
| **Sense** | news from GitHub, Hacker News, Anthropic, OpenAI, Gemini, Meta AI; boosted by your interests, `avoid` topics dropped | `src/omniscient/sense/` | `vault/news/` |
| **Think** | a visionary proposes ideas (showcase / new-build / upgrade tracks), a council debates and scores them | `src/omniscient/think/` | `vault/ideas/` |
| **Plan** | a strategist promotes ideas into a game plan within a WIP limit and writes wayfinder maps | `src/omniscient/plan/` | `vault/gameplan.md`, `vault/maps/` |
| **Build** | a builder agent makes the council's smallest version in a Docker box; optional web demo publishing | `src/omniscient/build/` | `pocs/<idea>/` |
| **Learn** | agents' kept notes become a tech knowledge base | `src/omniscient/learn/` | `vault/tech/learned/` |

Around them: `router.py` (task → model + effort), `registry/` (your projects),
`slack/` (digest and buttons), `studio/` (the herdr board and ateliers), `onboarding/`
(`omniscient init`). Agent definitions and studio skills ship inside the package
(`src/omniscient/resources/`).

### The home folder

The engine is the installed package; **your data lives in a home folder**, found as:
`$OMNISCIENT_HOME`, else the nearest parent of the current directory that holds
`config/omniscient.toml`, else `~/.omniscient`.

```
<home>/
  config/omniscient.toml   settings: budget, WIP limit, schedule, paths
  config/profile.toml      language, name, interests, avoid, GitHub user
  .env                     secrets (mode 600)
  vault/                   notes (open it in Obsidian): news, ideas, maps, tech
  repos/                   one markdown file per registered project
  pocs/                    prototypes, one git repo each
  agentrun/                optional overrides of the packaged agent definitions
  .claude/skills/          studio skills used by Claude Code in the ateliers
  .omniscient/             machine state: build logs, pages clone
```

### Main commands

```
omniscient init | studio | warroom | chat | schedule install|uninstall|status
omniscient idea list|show|context|new|variant|append|debate|verdict|todo|ui
omniscient ingest | learn-sync | think | plan | build STEM | work | grill | cycle
omniscient slack | slack-digest | slack-refresh | agents-push
omniscient group serve|status|token|widget|hooks install|uninstall|status
```

Optional systemd user units for a daily cycle and the Slack bot: see
[Daily schedule](#daily-schedule-systemd).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Tests run fully offline (`uv run pytest -q`).
Security issues: [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE).

## En français

Omniscient est une **usine à idées** : il lit l'actualité tech et IA, en tire des idées de
choses à construire, les fait débattre et noter par un conseil d'agents, planifie les
meilleures et construit des prototypes jetables dans un bac à sable Docker. Tu pilotes le
tout depuis le **studio**, dans herdr : un tableau d'idées et un atelier par idée avec
Claude Code.

```bash
uv tool install git+https://github.com/VinceVibeCoding/omniscient-factory
omniscient init       # dossier, profil (intérêts, langue), projets, clés
omniscient studio     # dans herdr
```

L'interface est en français pour l'instant ; les contributions pour l'anglais sont les
bienvenues. Le studio fonctionne sans clé API ; l'usine (news, idées, conseil, prototypes)
utilise ta clé Anthropic avec des plafonds journaliers appliqués dans le code.
