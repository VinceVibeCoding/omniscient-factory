---
name: omniscient-seed
description: Turn the owner's raw idea into an Omniscient idea note - ask at most two sharpening questions, link news sources and projects, create the note, then run the in-session council. Use when invoked as /omniscient-seed <raw idea text>.
---

You turn a raw thought from the owner into a well-formed idea in `vault/ideas/`, then debate it.

**Language:** talk to the owner in French; write the title and pitch in French. These instructions
are in English for you only.

**Argument:** `$ARGUMENTS` is the owner's raw idea, free text (French, possibly terse). If it is
empty, ask in French "Quelle est l'idée ?" and wait.

## 1. Find context before asking anything

Facts are your job, not the owner's. Look first:
- Duplicates and neighbours: `uv run omniscient idea list --json` (titles). If a close idea exists,
  say so and offer `/omniscient-riff '<its stem>'` or a variant instead of a duplicate.
- News that makes it possible now: `grep -ril '<term>' vault/news/` for 2-4 key terms (product names,
  techniques). Read the hits' frontmatter: keep the full `id:` of the relevant ones.
- The owner's projects it touches: `grep -il '<term>' repos/*.md`, then read those files. Use the
  `name:` from their frontmatter.

## 2. Sharpen: at most two questions, one round

Ask only what you cannot look up and what changes the idea's shape, usually among:
- **Pour qui ?** the user or buyer, if unclear.
- **Quelle preuve ?** what the first prototype should show (the one question it answers).
- **Nouveau ou amélioration ?** if it could be a new build or an upgrade of a project you found.

Ask them together, numbered, each with your recommended answer, worded so "oui" accepts it. Wait for
the owner's reply. Never answer for them. If the raw idea is already clear, ask nothing and go on.

## 3. Draft and confirm

Draft in French: a title (≤ 80 chars, concrete), a 2-4 sentence pitch (what it is, who it is for,
what the first prototype would show; add the "why now" capability if a source supports it), the kind
(`new-build` default, `showcase` for a demo of a new release, `upgrade` for ONE project, which then
needs `--project`), projects and source ids. Show it compactly and wait for the owner's OK or edits.

## 4. Create the seed

```bash
PITCH=$(cat <<'EOF'
<pitch>
EOF
)
TITLE=$(cat <<'EOF'
<titre>
EOF
)
uv run omniscient idea new --origin owner --title "$TITLE" --pitch "$PITCH" \
  --kind new-build --project <name> --source <news-id>
```

Repeat `--project` / `--source` once per value; drop them if there are none. Never invent a news id
or a project name. The command prints the new stem only: below it is `<new>`.

If the sharpening produced useful detail that is not in the pitch (audience, kill criteria, links to
other ideas as `[[<stem>]]`), log it:

```bash
uv run omniscient idea append '<new>' <<'EOF'
**Contexte de la graine :** ...
EOF
```

## 5. Debate it

Run the council flow in `.claude/skills/omniscient-riff/council.md` on `'<new>'`. Then offer
`/omniscient-riff '<new>'` to keep pushing it; the idea now appears on the studio board.

## Hard rules

- Change idea notes only via `uv run omniscient idea ...`; never edit `vault/ideas/*.md` directly.
- Never run `omniscient build|think|plan|cycle|work|grill|ingest` (API credit). The owner starts a
  build themselves.
- Never answer for the owner; never touch `.env` or secrets; never write to sibling repos.
