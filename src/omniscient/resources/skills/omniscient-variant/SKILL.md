---
name: omniscient-variant
description: Co-design a variant of an Omniscient idea with the owner (new audience, scope, tech or angle), fork it into its own note, then run the in-session council on it. Use when invoked as /omniscient-variant '<idea stem>' or when a riff calls for a variant.
---

You help the owner fork an existing idea into a variant: a sibling idea that keeps the parent's
sources, projects and kind but changes one meaningful thing.

**Language:** talk to the owner in French; write the title and pitch in French. These instructions
are in English for you only.

**Argument:** `$ARGUMENTS` is one quoted idea stem (e.g. `'2026-01-15 assistant-de-revue'`), the parent.
Keep the single quotes in shell commands. Below, `<stem>` means it.

## 1. Load the parent

```bash
uv run omniscient idea context '<stem>'
uv run omniscient idea list --json
```

If the stem is unknown, say so in French and stop. Note the parent's council (verdict, Challengeur,
Simplificateur, risks), its `## Atelier` entries if any, and its existing variants (cards whose
`parent` is `<stem>`) so you do not propose a duplicate.

## 2. Propose three variant directions

Each changes ONE axis, and is a real bet, not a cosmetic tweak:
- **Audience:** someone else with the same pain, or who would pay more.
- **Scope:** the 1/10 version (a weekend, one screen) or the 10x version (a platform).
- **Tech:** a different capability from recent news (`vault/news/`) that makes it cheaper or better.
- **Angle:** invert it, flip the business model, or attach it to one of the owner's projects
  (`repos/<name>.md`; read the relevant ones).

Favour directions that answer the council's objection or follow its Simplificateur line. Present
each in two lines (what changes, why it might win) and recommend one. Ask the owner to pick, mix or
adjust. Wait for their answer: never pick for them.

## 3. Write the pitch together

Draft a French pitch of 2-4 sentences: what it is, who it is for, what the first prototype would show.
Optionally a short title (otherwise the CLI uses `v<N> · <parent title>`). Show the draft; apply the
owner's edits until they say it is good ("ok", "vas-y", "c'est bon").

## 4. Fork it

Pass the French text through quoted heredocs so apostrophes and quotes are safe:

```bash
PITCH=$(cat <<'EOF'
<pitch>
EOF
)
TITLE=$(cat <<'EOF'
<titre>
EOF
)
uv run omniscient idea variant '<stem>' --pitch "$PITCH" --title "$TITLE"
```

Drop `--title` if the owner wants the default. The command prints the new stem only: below it is
`<new>`.

Then log the fork on the parent so the history is readable:

```bash
uv run omniscient idea append '<stem>' <<'EOF'
**Variante créée :** [[<new>]] — <ce qui change, en une ligne>
EOF
```

## 5. Debate the variant

Run the council flow in `.claude/skills/omniscient-riff/council.md` on `'<new>'` (the new stem,
quoted). Then compare in one or two French lines: variant score vs parent score, and what the
variant gained or lost.

## Hard rules

- Change idea notes only via `uv run omniscient idea ...`; never edit `vault/ideas/*.md` directly.
- Never run `omniscient build|think|plan|cycle|work|grill|ingest` (API credit). The owner starts a
  build themselves.
- Never answer for the owner; never touch `.env` or secrets; never write to sibling repos.
