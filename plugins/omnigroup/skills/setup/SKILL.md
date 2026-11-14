---
name: setup
description: Install or check Omnigroup (daemon, Claude Code hooks and statusLine, Windows widget, pairing) with one command. Use when the user runs /omnigroup:setup or asks to install the Omnigroup widget.
argument-hint: "[--protect] [--skip-widget]"
disable-model-invocation: true
allowed-tools: Bash(omniscient group setup), Bash(omniscient group setup --protect), Bash(omniscient group setup --skip-widget), Bash(omniscient group setup --protect --skip-widget), Bash(command -v omniscient)
---

# Omnigroup setup

1. Check the CLI exists: `command -v omniscient`. If it is missing, stop and tell the user to
   install it themselves, then rerun this skill:
   `uv tool install git+https://github.com/VinceVibeCoding/omniscient-factory`
2. Run the preview (it writes nothing): `omniscient group setup $ARGUMENTS`
3. Show the user the output as is, in particular the settings.json diff. Explain each step
   in one line (daemon, hooks + statusLine, widget download with SHA256 check, pairing).
4. Ask the user to confirm before writing. Only after an explicit yes, run
   `omniscient group setup --write $ARGUMENTS` (this Bash call needs their approval; do not
   add it to any allow list).
5. Report what changed (backup path of settings.json, where the installer was saved) and
   the remaining manual steps: start the daemon if it was down, run the installer on
   Windows, pair from the widget (Réglages → « Demander un code »).

Never print, copy or ask for tokens. Never edit ~/.claude/settings.json by hand: the
command shows a diff and keeps a backup.
