# omnigroup — les hooks des agents

omnigroup montre dans un widget Windows tous les agents en cours (Claude Code, GitHub
Copilot, herdr, AgentRun). Claude Code et Copilot y arrivent par leurs **hooks** : à
chaque événement, l'agent lance le hook omnigroup (`omniscient-hook`, la même chose que
`omniscient group hook` en plus rapide), qui transmet l'événement au
démon local (`omniscient group serve`, `127.0.0.1:7341`) et, pour une autorisation ou
une question, attend la réponse donnée dans le widget.

**Le widget d'abord, avec repli :** si le démon est arrêté, si le jeton manque, si aucun
widget n'a été connecté depuis 30 s ou si personne ne répond à temps, le hook se termine
sans rien écrire (code 0) et l'agent affiche son invite habituelle. Un hook omnigroup ne
bloque jamais un agent.

## Démarrer

```
omniscient group serve            # le démon (127.0.0.1:7341), Ctrl-C pour arrêter
omniscient group serve --no-collectors   # hooks seuls, sans herdr ni AgentRun
omniscient group serve --allow-terminal-pairing   # autorise le repli `pair --show-code`
omniscient group status           # le démon répond-il ? sessions et demandes ouvertes
omniscient group token            # où est le jeton des hooks (jamais affiché)
omniscient group pair             # comment coupler le widget
omniscient group pair --revoke    # oublier tous les widgets couplés
omniscient group widget           # où télécharger l'installateur Windows, couplage
```

Au démarrage de la session : `omniscient schedule install --write` écrit aussi
`omnigroup.service` (optionnel), à activer soi-même avec
`systemctl --user enable --now omnigroup.service` (Omniscient ne lance jamais
`systemctl`).

Widget : installateur NSIS dans les releases GitHub (tags `widget-v*`, avec
`SHA256SUMS` et une attestation de provenance) ou artefact `omnigroup-windows-nsis` du
workflow *Widget*. Couplage : voir plus bas.

## Modèle de menace

**Ce qu'on protège :** l'agent surveillé (Claude Code, Copilot) ne doit pas pouvoir
approuver ses propres demandes. Deux identifiants, deux pouvoirs :

| identifiant | où | permet | refusé (403) |
|---|---|---|---|
| jeton des **hooks** | `<dossier>/.omniscient/group.token` (600), lisible par l'utilisateur | `POST /hook`, puis `GET /pending/{id}/wait` **avec le ticket** que cet appel a renvoyé (aléatoire, à usage unique, lié à cette demande) | `/sessions*`, `/stream`, `/usage`, `/theme`, `/pending/*/answer`, `/sessions/*/prompt`, `/dismiss`, `/jump` |
| jeton du **widget** | Gestionnaire d'identification Windows (DPAPI) ; le démon n'en garde que le SHA-256 dans `group.db` | tout le reste : lire, répondre, écrire à un agent | `/hook`, `/pending/*/wait` |

- **Couplage** (`POST /pair/start`, sans identifiant, limité : un couplage à la fois,
  5 demandes par 10 min, code valable 2 min) : le démon affiche un code à 6 chiffres
  **uniquement sur le bureau Windows** (`powershell.exe -NoProfile -NonInteractive
  -Command <script fixe> <code>` : le code est un argument, jamais interpolé ; à défaut
  `msg.exe * /TIME:120`). Le widget envoie `POST /pair/finish {code, client_name}` :
  5 erreurs bloquent le couplage 10 min. Le jeton du widget est renvoyé une seule fois.
  Si Windows ne peut rien afficher, le couplage est refusé.
- **Repli plus faible** : `omniscient group serve --allow-terminal-pairing`, puis
  `omniscient group pair --show-code` dans un terminal **que vous ouvrez vous-même**. Le
  code s'affiche dans ce terminal, obtenu avec le jeton des hooks : un agent qui lit ce
  jeton pourrait faire de même et coupler un faux widget. À n'activer que le temps du
  couplage.
- **Le widget vérifie le démon** avant d'envoyer son jeton : `GET /health?nonce=N` doit
  contenir `HMAC-SHA256(clé = SHA-256(jeton), N)` ; un autre programme sur le port ne
  reçoit jamais le jeton. Pas de proxy, pas de redirection, flux SSE borné (ligne 64 Ko,
  événement 1 Mo).
- **Réponses en deux temps** : une réponse fait passer la demande `open → answering`
  (atomique) avant tout envoi ; une seconde réponse simultanée reçoit 409. Pour herdr, le
  texte du dialogue est relu juste avant les touches et doit avoir la même empreinte
  SHA-256 qu'à l'ouverture, le panneau doit être `blocked` ; sinon rien n'est envoyé.
- **Aperçu complet** : la carte montre toute l'entrée de l'outil (64 Ko max, police à
  chasse fixe, défilable), chemins compris ; les secrets sont masqués
  (`[secret masqué]`), les caractères de contrôle, bidi et de largeur nulle sont affichés
  en clair (`\u{202E}`), avec le titre et le dossier de la session. Au-delà de 64 Ko :
  « tronqué (N caractères) », seul « Refuser » reste possible (le démon refuse aussi
  « Autoriser »).
- **Fichiers** : dossier d'état 700, `group.db` (et `-wal`, `-shm`, `-journal`) et
  `group.token` 600, jeton écrit de façon atomique (fichier temporaire 600 + `replace`).
- **Limites** : 64 connexions simultanées, 8 flux `/stream`, attente d'un ticket ≥ 1 s,
  aperçus et charges utiles bornés (64 Ko par texte), 20 demandes listées par session.

**Risque résiduel, honnêtement :** un agent qui peut lancer n'importe quelle commande
Bash sous votre compte peut toujours lire `group.token`, modifier `group.db` (par exemple
y ajouter l'empreinte d'un jeton à lui), arrêter et relancer le démon, ou appeler
`powershell.exe` pour lire l'écran ou le coffre Windows de votre session. Les règles
`--protect` (ci-dessous) ne bloquent que les accès évidents (`Read`/`Edit`/`Write` du
dossier, `cat`, `sqlite3`… sur ses fichiers) : elles se contournent. La vraie barrière
reste de ne pas donner à un agent non surveillé un Bash sans restriction ; omnigroup
réduit la surface, il ne remplace pas un bac à sable.

**Chat du widget : le jeton du widget lance Claude.** `POST /chats` fait tourner
`claude -p` **sous votre compte**, dans un projet enregistré (`omniscient project list`,
ou le dossier Omniscient lui-même ; jamais un chemin envoyé par le widget). Détenir le
jeton du widget, c'est donc pouvoir faire travailler Claude Code dans ces projets.
Garde-fous :

- `--permission-mode default` est toujours passé : un `bypassPermissions` ou
  `acceptEdits` par défaut dans vos réglages ne s'applique jamais à un chat du widget.
  Chaque appel d'outil qui demande une autorisation passe par le hook
  `PermissionRequest` vers le widget ; sans hooks installés ou sans widget pour
  répondre, Claude en mode headless le **refuse**. Les règles `allow` de vos réglages
  restent valables : ne mettez pas `Bash(*)` dans `allow`.
- Budget : `--max-budget-usd 2` par tour et 30 tours par heure au plus (429 au-delà),
  2 tours simultanés, 30 min par tour.
- Messages bornés à 8 000 caractères, lignes stream-json à 1 Mo ; en cas d'échec, le
  widget reçoit un message générique, le détail (masqué) va dans le journal du démon.
- Prompt sur l'entrée standard, `argv` sans shell, identifiants vérifiés.

**Installeur du widget (`omniscient group setup --write`).** Téléchargé en HTTPS depuis
les hôtes GitHub uniquement (redirections comprises), accepté seulement si son SHA-256
correspond à `SHA256SUMS`. Ces deux fichiers viennent de la même release : le SHA-256
prouve l'intégrité, pas l'origine. Si `gh` est installé, l'attestation de provenance du
build est aussi vérifiée (`gh attestation verify --repo VinceVibeCoding/omniscient-factory`),
et un échec rejette le fichier ; sinon, setup le signale. Le fichier est écrit via un
nom temporaire unique et ne remplace jamais un lien symbolique.

**Confidentialité.** Le démon n'écoute que `127.0.0.1` ; seules les origines du widget
Tauri sont acceptées ; l'état reste dans un fichier SQLite local, les secrets masqués
dans les étapes et les demandes (jetons OpenAI, Anthropic, GitHub, Slack, Stripe, npm,
Google, AWS, JWT, en-têtes `Authorization`, `Bearer`, identifiants dans les URL,
`*_KEY=`, `*_TOKEN=`, `*_SECRET=`, `PASSWORD=`) et les chemins `/home/<nom>/` masqués
hors aperçus d'autorisation. Un hook peut autoriser ou refuser un appel d'outil, ajouter
**une règle exacte pour la session en cours seulement**, et répondre à
`AskUserQuestion` ; rien n'est jamais écrit dans vos réglages de permissions.

## Installer les hooks

```
omniscient group hooks status                      # ce qui est installé
omniscient group hooks install                     # aperçu (diff), rien n'est écrit
omniscient group hooks install --write             # sauvegarde, puis écriture
omniscient group hooks install --protect --write   # conseillé : + règles deny Claude
omniscient group hooks install --target claude --statusline --write   # + ligne d'état
omniscient group hooks uninstall --write           # retire uniquement nos entrées
```

- `--target claude|copilot|all` (défaut `all`) ; `--path FICHIER` pour un autre fichier
  (une seule cible), par exemple les réglages d'un projet `.claude/settings.json`.
- Sans `--write`, rien n'est modifié : le diff du fichier est affiché.
- Avec `--write` : sauvegarde horodatée à côté du fichier
  (`settings.json.omniscient-AAAAMMJJ-HHMMSS.bak`, jamais écrasée), puis remplacement
  atomique. Les droits du fichier sont conservés.
- Nos entrées sont reconnues à leur commande **exacte** (découpée comme un shell) : premier
  mot `…/omniscient-hook`, ou `<python> -m omniscient.group.hook_client`, ou
  `…/omniscient group hook` ; une simple mention (`echo omniscient-hook`) n'est pas à
  nous. Les autres hooks restent tels quels ; dans les réglages Claude seules les valeurs
  `hooks` (et `statusLine`, `permissions` si demandé) sont réécrites, le reste du fichier
  est conservé octet pour octet.
- Un `settings.json` qui est un lien symbolique est écrit dans sa cible réelle (le lien
  reste) ; le fichier temporaire est créé en 600 avec un nom unique dans le même dossier.
- `--protect` (Claude) ajoute à `permissions.deny` : `Read`, `Edit`, `Write` du dossier
  d'état (`~/.omniscient/**`, forme absolue `//chemin/**` hors du dossier personnel),
  `Bash(cat|less|more|head|tail|cp|sqlite3|strings|xxd <dossier>/*)` sous ses deux formes,
  `Bash(omniscient group pair*)` et `Bash(omniscient group token*)`. `uninstall` retire
  exactement ces règles ; les vôtres restent. Défense en profondeur seulement (voir
  « Modèle de menace »).
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

- « Toujours (cette session) » : `allow` + `updatedPermissions`, portée **session
  uniquement**, et la règle est affichée sur la carte avant le clic :
  1. les `permission_suggestions` de l'événement de type `addRules` / `allow` /
     destination `session` (jamais `setMode`, `addDirectories` ni une autre destination) ;
  2. sinon, pour `Bash`, `Write`, `Edit`, `MultiEdit`, `NotebookEdit` : une règle exacte,
     `{"toolName": "Bash", "ruleContent": "<commande>"}` ou le chemin absolu
     (`//chemin`), refusée si elle contient `* ? [ ]` ou un saut de ligne (elle couvrirait
     plus que la demande) — alors pas de bouton « Toujours » ;
  3. sinon le nom de l'outil seul (jamais pour les outils du point 2).
  Le hook recalcule la règle à partir de son propre événement : il ne la prend pas au
  démon. Syntaxe `//chemin` et correspondance exacte d'une commande **à vérifier** sur une
  vraie session.
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

La demande passe d'abord `open → answering` ; pour herdr et AgentRun, elle n'est marquée
« répondue » qu'après le succès du collecteur ; sinon le démon renvoie 502 et elle
redevient `open`. Une réponse pendant `answering` reçoit 409. herdr : seuls les panneaux
Claude sont pilotés au clavier ; pour les autres agents, la carte dit « réponds dans le
terminal » (correspondance des touches non vérifiée).

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
  pour répondre au dialogue d'un panneau ; les autres agents sont désactivés.
- Le message de couplage sous Windows (`powershell.exe` + `WScript.Shell.Popup`, repli
  `msg.exe`, absent des éditions Famille) et l'encodage des accents par l'interop WSL.
- Le stockage du jeton dans le Gestionnaire d'identification Windows (crate `keyring`
  3.6.3) : la CI compile et teste le widget mais ne le lance pas.
- Les règles `--protect` (syntaxe `~/…/**`, `//…/**`, `Bash(cat …/*)`) et la règle exacte
  « Toujours » sur une vraie session Claude Code.
- Les charges utiles et réponses des hooks Copilot.
- Le format des réponses `AskUserQuestion` (dont les choix multiples joints par `", "`).
- L'aller-retour `allow` complet sur une vraie session Claude Code (seul le repli
  « le hook n'écrit rien » a été vérifié). Les tests de bout en bout le couvrent hors
  ligne (démon réel, client de hook réel, charges utiles enregistrées), pas avec Claude.
