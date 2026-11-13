# omnigroup — les hooks des agents

omnigroup montre dans un widget Windows tous les agents en cours (Claude Code, GitHub
Copilot, herdr, AgentRun). Claude Code et Copilot y arrivent par leurs **hooks** : à
chaque événement, l'agent lance le hook omnigroup (`omniscient-hook`, la même chose que
`omniscient group hook` en plus rapide), qui transmet l'événement au
démon local (`omniscient group serve`, `127.0.0.1:7341`) et, pour une autorisation ou
une question, attend la réponse donnée dans le widget.

**Le widget d'abord, avec repli :** si le démon est arrêté, si le jeton manque ou si
personne ne répond à temps, le hook se termine sans rien écrire (code 0) et l'agent
affiche son invite habituelle. Un hook omnigroup ne bloque jamais un agent.

## Démarrer

```
omniscient group serve            # le démon (127.0.0.1:7341), Ctrl-C pour arrêter
omniscient group serve --no-collectors   # hooks seuls, sans herdr ni AgentRun
omniscient group status           # le démon répond-il ? sessions et demandes ouvertes
omniscient group token            # le jeton (créé s'il manque) : ce que lit le widget
omniscient group widget           # où télécharger l'installateur Windows, couplage
```

Au démarrage de la session : `omniscient schedule install --write` écrit aussi
`omnigroup.service` (optionnel), à activer soi-même avec
`systemctl --user enable --now omnigroup.service` (Omniscient ne lance jamais
`systemctl`).

Widget : installateur NSIS dans les releases GitHub (tags `widget-v*`) ou artefact
`omnigroup-windows-nsis` du workflow *Widget*. Il lit le jeton avec
`wsl.exe -e omniscient group token` ; sinon, collez la sortie de
`omniscient group token` dans ses réglages.

**Confidentialité.** Le démon n'écoute que `127.0.0.1` ; toutes les routes sauf
`/health` exigent le jeton (`<dossier>/.omniscient/group.token`, droits 600) ; seules les
origines du widget Tauri sont acceptées ; l'état reste dans un fichier SQLite local, les
secrets masqués dans les aperçus. Un hook peut autoriser ou refuser un appel d'outil,
ajouter une règle d'autorisation **pour la session en cours seulement**, et répondre à
`AskUserQuestion` ; rien n'est jamais écrit dans vos réglages de permissions.

## Installer les hooks

```
omniscient group hooks status                      # ce qui est installé
omniscient group hooks install                     # aperçu (diff), rien n'est écrit
omniscient group hooks install --write             # sauvegarde, puis écriture
omniscient group hooks install --target claude --statusline --write   # + ligne d'état
omniscient group hooks uninstall --write           # retire uniquement nos entrées
```

- `--target claude|copilot|all` (défaut `all`) ; `--path FICHIER` pour un autre fichier
  (une seule cible), par exemple les réglages d'un projet `.claude/settings.json`.
- Sans `--write`, rien n'est modifié : le diff du fichier est affiché.
- Avec `--write` : sauvegarde horodatée à côté du fichier
  (`settings.json.omniscient-AAAAMMJJ-HHMMSS.bak`, jamais écrasée), puis remplacement
  atomique. Les droits du fichier sont conservés.
- Nos entrées sont reconnues à leur commande (`omniscient-hook`,
  `omniscient.group.hook_client` ou l'ancienne forme `omniscient group hook`). Les autres hooks
  restent tels quels ; dans les réglages Claude seule la valeur `hooks` (et `statusLine`
  si demandé) est réécrite, le reste du fichier est conservé octet pour octet.
- La commande installée est le chemin absolu du script `omniscient-hook` (trouvé dans
  le `PATH`), à défaut `<python> -m omniscient.group.hook_client`, suivie de
  `--home DOSSIER` si votre dossier Omniscient n'est pas `~/.omniscient`.

### Claude Code — `~/.claude/settings.json`

| Événement | matcher | délai | rôle |
|---|---|---|---|
| `PermissionRequest` | `*` | 110 s | attend la décision (100 s max) |
| `PreToolUse` | `^AskUserQuestion$` | 110 s | attend la réponse à la question |
| `PreToolUse` | `*` | 5 s | suivi (`--fast`), ignore AskUserQuestion |
| `PostToolUse`, `PostToolUseFailure` | `*` | 5 s | suivi |
| `SessionStart`, `SessionEnd`, `UserPromptSubmit`, `Notification`, `Stop`, `SubagentStop` | — | 5 s | suivi |

Les entrées de suivi (`--fast`) envoient l'événement (1 s max) et rendent la main.

Réponses écrites par le hook :

```json
{"hookSpecificOutput": {"hookEventName": "PermissionRequest",
  "decision": {"behavior": "allow"}}}
```

- « Toujours (cette session) » : `allow` + `updatedPermissions: [{"type": "addRules",
  "rules": [{"toolName": T}], "behavior": "allow", "destination": "session"}]` —
  portée **session uniquement**, rien n'est enregistré dans vos réglages.
- Refus : `{"behavior": "deny", "message": "Refusé depuis omnigroup", "interrupt": false}`.
- Question (`AskUserQuestion`) : `{"hookSpecificOutput": {"hookEventName": "PreToolUse",
  "permissionDecision": "allow", "updatedInput": {"questions": [...], "answers":
  {"question": "libellé"}}}}` ; à choix multiples, les libellés sont joints par `", "`
  (**à vérifier** avec une vraie session : `claude -p` ne propose pas AskUserQuestion).

### Ligne d'état (option)

`--statusline` règle la `statusLine` de Claude Code sur `omniscient-hook --statusline`
(équivalent de `omniscient group statusline`).
Elle affiche `omnigroup · modèle · contexte N % · 5 h N % · 7 j N %` et transmet au
démon le JSON de la ligne d'état, qui est la seule source **documentée** du coût
(`cost.total_cost_usd`), de la taille de la fenêtre de contexte
(`context_window.context_window_size`) et de l'usage de l'abonnement
(`rate_limits.five_hour` / `seven_day` : `used_percentage`, `resets_at`). Une
`statusLine` existante n'est remplacée qu'avec `--force` (après sauvegarde) ;
`uninstall` ne retire que la nôtre.

### GitHub Copilot — `~/.copilot/hooks/omnigroup.json`

Un fichier à nous (`{"version": 1, "hooks": {...}}`), entrées
`{"type": "command", "bash": "... --source copilot --event <nom>", "timeoutSec": 30}`
pour `sessionStart`, `sessionEnd`, `userPromptSubmitted`, `preToolUse`, `postToolUse`,
`permissionRequest` (attend 25 s max) et `agentStop`. Le nom de l'événement est passé en
`--event` car la charge utile Copilot ne le contient pas toujours. Réponse à
`permissionRequest` : `{"behavior": "allow"}` ou `{"behavior": "deny", "message": ...}`
(« toujours » = `allow`, Copilot n'a pas de règle de session). `uninstall` supprime le
fichier s'il ne contient plus que nos entrées. Format des événements et réponses Copilot
**à vérifier** sur une vraie session.

**Copilot lit aussi `.claude/settings.json`.** Quand le hook Claude reçoit une charge
utile Copilot (`sessionId` en camelCase, pas de `session_id`) :
- si le fichier Copilot d'omnigroup est installé, il ne fait rien (le hook Copilot
  rapporte l'événement : une seule fois) ;
- sinon il le rapporte comme session Copilot, en suivi seulement (pas de décision).

## Usage et contexte

Les hooks ne portent pas l'usage, seulement `transcript_path`. Le démon (jamais le hook)
appelle `hook_usage.translate_usage(source, event, cursor)` à chaque `POST /hook` (seuls
`Stop`, `PostToolUse`, `SubagentStop`, `Notification` et la ligne d'état comptent), avec
**un curseur par session gardé en mémoire** (oublié à `SessionEnd`), et enregistre
l'usage quand il est connu. Un rafraîchissement de la ligne d'état ne met à jour que
l'usage (ni état, ni étape) et seulement pour une session déjà connue :

- il lit le transcript JSONL à partir d'un curseur par session (décalage en octets :
  chaque octet est lu une fois ; un transcript jamais vu n'est lu que sur ses derniers
  256 Ko ; 8 Mo max par appel ; une ligne à moitié écrite attend l'appel suivant) ;
- les réponses de la conversation principale (pas `isSidechain`) sont additionnées
  (`input`, `output`, `cache_read`, `cache_creation`), une réponse répétée sur
  plusieurs lignes (même `message.id`) ne compte qu'une fois ;
- contexte = `input + cache_read + cache_creation` du dernier appel ; taille de la
  fenêtre = la valeur de la ligne d'état si connue, sinon 200 000 (1 000 000 si le modèle
  contient `[1m]` ou si le contexte dépasse 200 000) — table par modèle **à vérifier** ;
- le coût et l'usage de l'abonnement restent vides sans la ligne d'état (le transcript
  ne les documente pas) ; pour Copilot l'usage reste vide.

Le format du transcript est interne à Claude Code : une ligne inconnue est ignorée, et
une erreur garde les chiffres précédents.

## Format des réponses

Le widget, le démon (`POST /pending/{id}/answer`) et les hooks/collecteurs s'accordent :

| demande | corps envoyé | effet |
|---|---|---|
| permission | `{"decision": "allow"\|"deny"\|"always"}` | JSON du hook (ci-dessus) ; herdr : touches |
| question à choix | `{"answers": {question: libellé}}`, choix multiples joints par `", "` | `updatedInput.answers` |
| question AgentRun (texte libre) | `{"answers": {question: texte}}` (4 000 caractères max) | `POST /v2/runs/{id}/messages {"content": texte}` |
| budget AgentRun | `{"choice": "raise"\|"stop"}` | `PATCH max_cost_usd` ou `POST /stop` |

Pour herdr et AgentRun, la demande n'est marquée « répondue » qu'après le succès du
collecteur ; sinon le démon renvoie 502 et elle reste ouverte.

## Performance

Une entrée de suivi doit rendre la main vite. Mesuré (médiane de 7, sans démon) :
`omniscient-hook` démarre en ~61 ms, `python -m omniscient.group.hook_client` ~62 ms,
la CLI complète `omniscient group hook` ~394 ms (elle charge toute l'application) ;
`python -c pass` ~9 ms. C'est pourquoi l'installateur écrit `omniscient-hook`.

## Comportement vérifié

Vérifié avec Claude Code 2.1.323, `claude -p --output-format json`, dans un dossier
temporaire avec des réglages **de projet** (`.claude/settings.json`), jamais les réglages
globaux :

- **`PermissionRequest` se déclenche en mode `-p`.** Charge utile reçue : `session_id`,
  `transcript_path`, `cwd`, `prompt_id`, `permission_mode` (`default`), `effort`,
  `hook_event_name`, `tool_name` (`Bash`), `tool_input` (`command`, `description`),
  `permission_suggestions` (ex. `addDirectories`, `setMode`, avec `destination`
  `session`).
- **Un hook qui n'écrit rien laisse le comportement normal** : en `-p`, sans invite
  possible, l'outil est refusé (il apparaît dans `permission_denials`) et l'agent
  l'explique. C'est bien le repli attendu.
- **La réponse `allow` n'a pas pu être vérifiée ici** : l'appel de vérification a été
  bloqué par le classifieur de sécurité de la session d'outillage. À refaire à la main
  (même dossier, hook qui imprime le JSON `allow` ci-dessus, puis
  `claude -p "Lance mkdir test_marker avec Bash"`) : le dossier doit être créé.
- Le JSON de la ligne d'état (`cost`, `context_window`, `rate_limits.five_hour|seven_day`
  avec `resets_at` en secondes epoch) est celui documenté dans Claude Code 2.1.323.

## Non vérifié

- La correspondance des touches herdr (claude : autoriser = `enter`, refuser = `esc`)
  pour répondre au dialogue d'un panneau.
- Les charges utiles et réponses des hooks Copilot.
- Le format des réponses `AskUserQuestion` (dont les choix multiples joints par `", "`).
- L'aller-retour `allow` complet sur une vraie session Claude Code (seul le repli
  « le hook n'écrit rien » a été vérifié). Les tests de bout en bout le couvrent hors
  ligne (démon réel, client de hook réel, charges utiles enregistrées), pas avec Claude.
