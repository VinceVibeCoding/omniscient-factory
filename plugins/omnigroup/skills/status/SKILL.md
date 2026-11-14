---
name: status
description: Show whether the Omnigroup daemon runs, how many sessions it follows, and the widget settings. Use when the user runs /omnigroup:status or asks if Omnigroup works.
disable-model-invocation: true
allowed-tools: Bash(omniscient group status), Bash(omniscient group config get), Bash(omniscient group hooks status)
---

# Omnigroup status

Run these three read-only commands and summarise them in a few lines:

1. `omniscient group status` — daemon up or down, sessions followed, pending requests.
2. `omniscient group hooks status` — whether the Claude Code hooks and statusLine are in.
3. `omniscient group config get` — theme, OSD view, ticker, and where each value comes
   from (default, config.json or settings.json).

If the daemon is down, suggest `omniscient group serve`; if hooks are missing, suggest
`/omnigroup:setup`. Do not change anything.
