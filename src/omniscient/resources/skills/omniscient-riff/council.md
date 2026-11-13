# Council flow (shared by every omniscient-* skill)

Run an in-session council debate on ONE idea stem, then record it with `omniscient idea debate`.
It must score like the factory's council (agentrun/council.json), so in-session ideas rank
fairly against the daily cycle's ideas on the same board.

## 1. Load the idea fresh

```bash
uv run omniscient idea context '<stem>'
```

Read the note, its sources and the linked `repos/<project>.md` files. Debate THIS text, not your memory
of the conversation. If the note has an `## Atelier` section, take its conclusions into account.

## 2. Play four distinct members, out loud, in French

Show the debate to the owner, one short paragraph per member:

1. **Visionnaire**: the strongest version of the idea. What does it unlock? Why now (which new
   capability or release makes it newly possible or cheap)? Who would love it?
2. **Challengeur**: the strongest honest objection. Is it really new? Does it already exist (name
   the competitors you know)? Is "why now" real? Would anyone use it, or pay for it?
3. **Simplificateur (plus petite version)**: the smallest version that still proves the core idea.
   One prototype that answers ONE question in days, with the question stated, and what result would
   mean "go" or "stop". If the shape is wrong, say here how to reshape it.
4. **Gardien des risques**: what could break. Dependencies, platform limits, cost, privacy and data,
   legal or terms of service, conflicts with the owner's projects (shared ports, shared data,
   breaking changes; check `repos/*.md`).

## 3. Score, calibrated

Each score is 0..1, two decimals:
- `novelty`: how new versus what exists (0.3 = known category with a twist; 0.8 = few have tried it).
- `impact`: value if it works, for the owner or real users.
- `feasibility`: can a prototype answer the core question within days with today's tools.
- `fit`: fit with the owner's projects (`repos/`) and skills. A standalone idea is usually ≤ 0.4.
- `effort`: **1 = a lot of work**, 0 = an afternoon. (The weighting inverts it; never invert it yourself.)

Be calibrated. Most ideas are not 0.9 on anything; factory ideas usually land between 0.2 and 0.7.
The owner is in the room and may love the idea: do NOT inflate scores to please them. Score the
note as written, exactly as a stranger council would.

Verdict, one of exactly three French words: `poursuivre`, `remodeler` (good core, wrong shape: say
how in the Simplificateur line), `abandonner`.

## 4. Write the council markdown (exact labels: Slack parses them)

The labels come from `src/omniscient/i18n.py`; copy them byte for byte, including the space
before the colon. The verdict word must come right after `**Verdict :**`. The Visionnaire is
spoken in-session only; it is not a section of the note.

```markdown
**Verdict :** remodeler

**Challengeur :** <objection en une à quatre phrases>

**Simplificateur (plus petite version) :** <le plus petit prototype, la question unique, le critère go/stop>

**Gardien des risques :**
- <risque 1>
- <risque 2>
```

If no risk is raised, the risks body is exactly `- aucun risque soulevé`.
Never put `#` or `##` headings inside the council (the tool demotes them to `###`); use bold
labels and lists. The same goes for `idea append` text and pitches.

## 5. Record it

Pipe ONE JSON object on stdin. `council` is the markdown above as a JSON string (newlines as `\n`,
double quotes escaped as `\"`); `scores` has exactly the five keys:

```bash
uv run omniscient idea debate '<stem>' <<'EOF'
{"council": "**Verdict :** remodeler\n\n**Challengeur :** Le créneau existe déjà (Otterly, Peec AI) ; la vraie différence reste à prouver.\n\n**Simplificateur (plus petite version) :** Un script sur 10 commerces saisis à la main dans un JSON : le taux d'erreur factuelle des assistants dépasse-t-il 20 % ? Oui → on continue, non → on abandonne.\n\n**Gardien des risques :**\n- Conditions d'utilisation des API à vérifier pour la surveillance systématique.\n- Réponses non déterministes : il faut répéter les requêtes.", "scores": {"novelty": 0.35, "impact": 0.5, "feasibility": 0.7, "fit": 0.2, "effort": 0.4}}
EOF
```

The command prints the weighted score. Recording has consequences, so say them to the owner:
- `poursuivre` / `remodeler`: a seed becomes `debated`; at or above the promote threshold
  (0.55 in `config/omniscient.toml`) the next 07:00 cycle may plan it, which spends API credit.
- `abandonner`: a seed or debated idea becomes `dropped`, exactly like the factory council.
A verdict other than these three words is refused. On an error (French message, exit 1), fix the JSON and retry;
never edit the note by hand.

## 6. Report to the owner (in French)

Give the score, the verdict and the smallest version in three lines. Then offer next moves without
taking them:
- `poursuivre`: the PoC build costs up to $1.50 of API credit, so only the owner starts it. If this
  stem is the atelier's own idea, press `b` in the « UI » pane (it asks to confirm the cost). For a new
  stem (variant, remix, seed), tell them to open its atelier from the studio board (Entrée) or to
  run `uv run omniscient build '<stem>'` themselves. Never run `omniscient build` yourself.
- `remodeler`: offer a variant (`/omniscient-variant '<stem>'`) shaped by the Simplificateur line.
- `abandonner`: the idea is now `dropped`. If the owner disagrees, offer a variant that answers the
  Challengeur (the variant gets its own council). The owner's verdict (`pursue|reshape|drop`) is
  always theirs: never pick it for them.
