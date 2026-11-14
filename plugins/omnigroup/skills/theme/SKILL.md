---
name: theme
description: Switch the Omnigroup widget theme (default, terminal, dashboard). Use when the user runs /omnigroup:theme or asks to change the widget skin.
argument-hint: "<default|terminal|dashboard>"
disable-model-invocation: true
allowed-tools: Bash(omniscient group config set theme default), Bash(omniscient group config set theme terminal), Bash(omniscient group config set theme dashboard), Bash(omniscient group config get)
---

# Omnigroup theme

The argument is `$ARGUMENTS`.

1. It must be exactly one of `default`, `terminal`, `dashboard`. If it is empty or anything
   else, list the three themes (default: Omniscient navy; terminal: green-on-black htop
   vibe; dashboard: Bloomberg-terminal cool blue) and ask which one, without running
   anything.
2. Run `omniscient group config set theme <theme>`. The widget switches live; the choice is
   kept in `~/.config/omnigroup/config.json`.
3. If the command says the key is locked, the theme is pinned by `env.OMNIGROUP_THEME` in
   `~/.claude/settings.json`: tell the user to change or remove that value there (do not
   edit the file yourself).
