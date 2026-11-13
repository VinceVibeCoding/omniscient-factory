# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.
This is a single-context layout. The glossary and ADRs are optional: when they exist, they live inside the
Obsidian vault of your Omniscient home. A fresh home has neither.

## Before exploring, read these if they exist

- **`vault/glossary/GLOSSARY.md`** (optional): the shared vocabulary.
- **`vault/decisions/`** (optional): ADRs. Read the ones that touch the area you're about to work in.

If either is missing or empty, **proceed silently**. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) fills them lazily as terms and decisions get resolved. When it would create `GLOSSARY.md` or `docs/adr/` at the repo root, use the vault paths above instead.

## File structure (once created)

```
/
├── vault/glossary/GLOSSARY.md
├── vault/decisions/
│   ├── 0001-<decision>.md
│   └── 0002-<decision>.md
└── src/omniscient/
```

## Use the glossary's vocabulary

When your output names a domain concept (an issue title, a refactor proposal, a hypothesis, a test name), use the term as `GLOSSARY.md` defines it. Don't drift to synonyms the glossary explicitly avoids.

If the concept you need isn't in the glossary yet, that's a signal. Either you're inventing language the project doesn't use (reconsider), or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, say so explicitly rather than silently overriding it:

> _Contradicts ADR-0007 (event-sourced orders), but worth reopening because…_
