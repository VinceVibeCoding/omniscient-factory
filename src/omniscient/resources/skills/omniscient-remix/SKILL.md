---
name: omniscient-remix
description: Combine 2-3 Omniscient ideas into a new one - read them, propose three combined concepts, let the owner pick or adjust, create the remix note, then run the in-session council. Use when invoked as /omniscient-remix '<stem1>' '<stem2>' ['<stem3>'].
---

You help the owner remix two or three ideas into a new one that is more than their sum.

**Language:** talk to the owner in French; write the title and pitch in French. These instructions
are in English for you only.

**Arguments:** `$ARGUMENTS` holds 2 or 3 quoted idea stems, e.g.
`'2026-01-15 assistant-de-revue' '2026-01-15 tableau-de-bord-vocal'`. Keep each stem's single quotes in
shell commands. If there are fewer than 2 or more than 3, say so in French and stop.

## 1. Load every parent

For each stem:

```bash
uv run omniscient idea context '<stemN>'
```

If one is unknown, say which in French and stop. For each parent, extract: the core mechanism (what
it actually does), the audience, the "why now" capability, its sources and projects, and what its
council liked or objected to.

## 2. Propose three combined concepts

Each concept must be a real fusion, not "A + B side by side". Use different recipes, for example:
- **Mechanism × audience:** A's engine serving B's users.
- **Shared core:** both ideas need the same hidden piece; build that piece as the product.
- **Pipeline:** A's output is B's input; the chain is the product.
- **Answer the objections:** one parent's strength fixes the other's council objection.
- **Owner leverage:** the combination plugs into one of the owner's projects (`repos/<name>.md`).

For each concept, in French: a working title, two sentences (what it is, for whom), what it keeps
from each parent, and the smallest test that would prove it. Recommend one. Ask the owner to pick,
merge or adjust; wait for their answer. Never pick for them.

## 3. Finalise with the owner

Draft the title and a 2-4 sentence pitch (what it is, who it is for, what the first prototype would
show). Choose with the owner:
- `--kind`: `new-build` (default), `showcase` (demo of a new release), or `upgrade` (applies to ONE
  owner project; then `--project` is required).
- `--project`: project names from `repos/` (the `name:` in `repos/<file>.md`), union of what is
  relevant, possibly none.
- `--source`: news ids (the full `id:` in `vault/news/*.md` frontmatter) from the parents that still
  apply. Never invent ids.

Show the final draft and wait for the owner's OK.

## 4. Create the remix

```bash
PITCH=$(cat <<'EOF'
<pitch>
EOF
)
TITLE=$(cat <<'EOF'
<titre>
EOF
)
uv run omniscient idea new --origin remix --parent '<stem1>' --parent '<stem2>' \
  --title "$TITLE" --pitch "$PITCH" --kind new-build --project <name> --source <news-id>
```

Repeat `--parent`, `--project`, `--source` once per value; drop the flags that have no value. The
command prints the new stem only: below it is `<new>`.

Log the remix on each parent:

```bash
uv run omniscient idea append '<stemN>' <<'EOF'
**Remixée dans :** [[<new>]] — <ce que cette idée y apporte, en une ligne>
EOF
```

## 5. Debate the remix

Run the council flow in `.claude/skills/omniscient-riff/council.md` on `'<new>'`. Then tell the owner
in French how it scores against its parents, and offer `/omniscient-riff '<new>'` to keep pushing it.

## Hard rules

- Change idea notes only via `uv run omniscient idea ...`; never edit `vault/ideas/*.md` directly.
- Never run `omniscient build|think|plan|cycle|work|grill|ingest` (API credit). The owner starts a
  build themselves.
- Never answer for the owner; never touch `.env` or secrets; never write to sibling repos.
