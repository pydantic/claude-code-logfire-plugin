---
description: Remove the stored Logfire OAuth token
allowed-tools: Bash(logfire-auth *)
---

Delete the stored OAuth token bundle at `~/.logfire/claude-code-logfire-plugin.json`.
The bundle's own base URL is used — no extra argument needed. After logout the
plugin falls back to `LOGFIRE_TOKEN` (or stays silent if no token is configured).

!`logfire-auth logout`
