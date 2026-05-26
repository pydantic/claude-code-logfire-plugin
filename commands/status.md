---
description: Show stored Logfire OAuth token status (expiry, scope, etc.)
allowed-tools: Bash(logfire-auth *)
---

Print metadata about the stored OAuth token bundle: the base URL, scope,
client_id, time until expiry, and whether a refresh token is present. Does not
display the access token itself. Operates on whatever's in the store — only one
bundle is kept at a time.

!`logfire-auth status`
