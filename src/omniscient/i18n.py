"""French user-facing vocabulary (the owner and users are French-speaking).

Machine-read strings stay English on purpose: frontmatter keys and statuses, JSON keys
and enums, and the wayfinder map headings / ticket lines that the /wayfinder skill parses.
"""

from __future__ import annotations

KIND_FR = {"showcase": "Démo", "new-build": "Nouveau projet", "upgrade": "Amélioration"}
VERDICT_FR = {"pursue": "poursuivre", "reshape": "remodeler", "drop": "abandonner"}

# Idea note body
H_PROJECTS = "## Projets"
H_SOURCES = "## Sources"
H_COUNCIL = "## Conseil"
H_DISCUSSION = "## Discussion"
COUNCIL_PENDING = "_Débat en attente._"
WHY_NOW = "**Pourquoi maintenant :**"
NONE = "_aucun_"
NEW_BUILD = "nouveau projet"

# Council section (labels also parsed back by the Slack digest)
L_VERDICT = "**Verdict :**"
L_CHALLENGER = "**Challengeur :**"
L_SIMPLIFIER = "**Simplificateur (plus petite version) :**"
L_RISKS = "**Gardien des risques :**"
NO_RISK = "- aucun risque soulevé"

# Discussion lines
L_OWNER = "**Propriétaire :**"
L_DEBATER = "**Débatteur :**"
