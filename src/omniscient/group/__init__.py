"""omnigroup: every running agent (Claude Code, Copilot, herdr, AgentRun) in one place.

A local daemon (`omniscient group serve`) collects sessions from agent hooks and pollers
into a SQLite store and serves them over HTTP + SSE to the Windows corner widget, which
can approve permissions, answer questions and jump back to a session.
"""
