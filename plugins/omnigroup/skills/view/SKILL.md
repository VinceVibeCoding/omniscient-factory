---
name: view
description: Switch the Omnigroup OSD layout between bars and gauge. Use when the user runs /omnigroup:view or asks to change the OSD view.
argument-hint: "<bars|gauge>"
disable-model-invocation: true
allowed-tools: Bash(omniscient group config set osd_view bars), Bash(omniscient group config set osd_view gauge), Bash(omniscient group config get)
---

# Omnigroup OSD view

The argument is `$ARGUMENTS`.

1. It must be exactly `bars` (default, with the scrolling cost ticker) or `gauge` (two
   rings, car-dashboard style). Otherwise list both and ask, without running anything.
2. Run `omniscient group config set osd_view <view>`. The widget switches live; the same
   choice is available on the widget with right-click → OSD View.
3. If the key is locked, it is pinned by `env.OMNIGROUP_OSD_VIEW` in
   `~/.claude/settings.json`: tell the user to change it there.
