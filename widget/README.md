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
2. Dans le widget : ⚙ Réglages → **Coupler via WSL**. Le widget lance
   `wsl.exe [-d <distribution>] -e bash -lc "omniscient group token"` (shell de connexion,
   donc `~/.local/bin` est dans le PATH), lit le jeton et l'enregistre.
3. Sinon : lancez `omniscient group token` dans WSL et collez le jeton dans le champ
   **Jeton**, puis **Enregistrer**.

Les réglages (adresse, jeton, distribution, commande) sont stockés dans le dossier de
configuration de l'application (`%APPDATA%\dev.omniscient.omnigroup\settings.json`).
L'adresse du démon est limitée à `http://127.0.0.1`, `localhost` ou `[::1]` : le jeton ne
quitte jamais la machine. Un jeton refusé (401/403) affiche « recouplez le widget ».

## Développer

```
cd widget
npm ci
npm run typecheck && npm run build    # partie web
npm run tauri dev                      # Windows (Rust + WebView2 requis)
cd src-tauri && cargo test             # tests Rust (parseur SSE, réglages, réponses…)
```

Toute la partie réseau (HTTP + SSE avec reconnexion) est en Rust ; la webview ne reçoit
que des événements Tauri (`group://event`, `group://status`) et n'a que la permission
`core:default`. `Cargo.lock` est généré au premier build CI (artefact
`omnigroup-cargo-lock`) : committez-le pour figer les dépendances Rust.

Icônes : `npx tauri icon icon.svg -o src-tauri/icons`.
