---
description: Force-refresh the stored Logfire OAuth access token
allowed-tools: Bash(logfire-auth *)
---

Force an OAuth refresh against the Logfire authorization server using the
stored refresh token, even if the access token isn't near expiry. Operates on
the stored bundle's own base URL. The plugin's hooks already refresh lazily on
every invocation, so this is mostly useful for debugging or to deliberately
cycle the access token without re-running the device flow.

!`logfire-auth refresh`
