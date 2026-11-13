# Omnigroup — widget Windows

Petit widget de coin d'écran (Tauri v2) qui montre tous les agents en cours d'une home
Omniscient — Claude Code, GitHub Copilot, herdr, AgentRun — et permet d'autoriser, de
répondre, de revenir à la session ou de la rayer sans ouvrir VS Code.

- Réduit : une pastille (œil + ● en cours, ◆ en attente, ✓ terminés, total de jetons/coût).
- Ouvert (360×520) : liste des sessions (jauge de contexte), détail avec étapes, cartes
  d'attente (permission, question, budget, approbation externe) et bloc « Consommation ».
- Icône dans la zone de notification (afficher / masquer, quitter) ; notification native
  quand une session passe « en attente ».
- Couleurs : `GET /theme` du démon (thème Omniscient choisi par `omniscient theme set`).

## Installer

Téléchargez l'installeur NSIS (`Omnigroup_<version>_x64-setup.exe`) depuis l'artefact
`omnigroup-windows-nsis` du workflow **Widget**, ou depuis la release brouillon d'un tag
`widget-v*`. L'installation se fait pour l'utilisateur courant.

## Coupler avec le démon (WSL)

1. Dans WSL : `omniscient group serve` (écoute sur `127.0.0.1:7341` uniquement ; Windows
   y accède par le transfert localhost de WSL2 ou le mode réseau « mirrored »).
2. Dans le widget : ⚙ Réglages → **Demander un code**. Le démon affiche un code à
   6 chiffres **sur le bureau Windows** (message natif via `powershell.exe`, à défaut
   `msg.exe`) — jamais dans WSL ni dans la réponse HTTP.
3. Tapez le code dans le widget → **Coupler** (2 minutes, 5 essais, puis blocage 10 min).
   Le démon renvoie un jeton de widget une seule fois et n'en garde que l'empreinte
   SHA-256 ; le widget le range dans le **Gestionnaire d'identification Windows**
   (chiffré par DPAPI pour votre compte), jamais dans `settings.json` ni dans la webview.
4. Si Windows ne peut pas afficher le code : voir `omniscient group pair` (repli plus
   faible, documenté dans `docs/omnigroup.md`).

Avant chaque connexion, le widget vérifie le démon sans lui envoyer son jeton
(`GET /health?nonce=…` doit renvoyer HMAC-SHA256(SHA-256(jeton), nonce)) ; un autre
programme sur le port n'obtient donc jamais le jeton. **Oublier le couplage** efface le
jeton local ; `omniscient group pair --revoke` les révoque côté démon.

Les réglages ne contiennent plus que l'adresse du démon
(`%APPDATA%\dev.omniscient.omnigroup\settings.json`) ; un ancien fichier qui contenait un
jeton est réécrit sans lui au démarrage. L'adresse est limitée à `http://127.0.0.1`,
`localhost` ou `[::1]`, sans proxy ni redirection.

## Développer

```
cd widget
npm ci
npm run typecheck && npm run build    # partie web
npm run tauri dev                      # Windows (Rust + WebView2 requis)
cd src-tauri && cargo test             # tests Rust (SSE bornée, preuve, réglages…)
```

Toute la partie réseau (HTTP + SSE avec reconnexion) est en Rust ; la webview ne reçoit
que des événements Tauri (`group://event`, `group://status`) et n'a que la permission
`core:default`. `Cargo.lock` est généré au premier build CI (artefact
`omnigroup-cargo-lock`) : committez-le pour figer les dépendances Rust.

Icônes : `npx tauri icon icon.svg -o src-tauri/icons`.
