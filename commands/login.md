---
description: Log in to Logfire via OAuth device flow (alternative to LOGFIRE_TOKEN)
allowed-tools: Bash(logfire-auth *)
---

Run the OAuth device authorization flow against Logfire and persist a token
bundle the plugin will auto-refresh on every Claude Code session.

Use this instead of setting a fixed `LOGFIRE_TOKEN`. Once logged in, the plugin
silently refreshes the access token whenever it's near expiry (no manual
re-login until the refresh token itself is revoked).

The script reads `$LOGFIRE_BASE_URL` for the default region (US if unset). Any
slash-command arguments are forwarded, so you can override with
`--base-url http://localhost:3000` (or pass `--no-browser`, `--client-id ...`,
etc). The chosen base URL is recorded inside the stored bundle, so subsequent
`/logfire-session-capture:logout`, `:status`, `:refresh` don't need any
arguments — only one base URL is stored at a time, and re-running login
overwrites the previous bundle.

!`logfire-auth login $ARGUMENTS`
