"""OAuth token storage and refresh for the Logfire plugin.

Stdlib-only helpers shared by ``auth.py`` (interactive device flow login) and
``log-event.py`` (hot-path hook handler that needs a valid access token).

A single token "bundle" is persisted at
``~/.logfire/claude-code-logfire-plugin.json`` with mode 0600:

```json
{
  "base_url": "https://logfire-us.pydantic.dev",
  "access_token": "...",
  "refresh_token": "...",
  "expires_at": 1234567890.0,
  "scope": "project:write_otlp",
  "client_id": "https://logfire.pydantic.dev/clients/claude-code-logfire.json",
  "resource": "https://logfire-us.pydantic.dev/v1",
  "token_endpoint": "https://logfire-us.pydantic.dev/oauth/token"
}
```

``token_endpoint`` is cached from the RFC 8414 metadata at login (or
re-populated on the first refresh of a legacy bundle) so the hot-path
refresh doesn't have to make a discovery roundtrip before the token POST.

Only one bundle is ever stored — logging in to a different ``base_url``
overwrites the previous one. ``logout`` / ``status`` / ``refresh`` therefore
don't need a ``--base-url`` argument; they always operate on the stored
bundle's own ``base_url``.

The file is rewritten atomically (``mkstemp`` + ``os.replace``) and refresh is
serialised across sessions via an ``os.mkdir`` lock to keep refresh-token
rotation from racing when several Claude Code sessions start simultaneously.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://logfire-us.pydantic.dev"
DEFAULT_SCOPE = "project:write_otlp"


def _read_plugin_version() -> str:
    """Read the plugin version from ``.claude-plugin/plugin.json``.

    Single source of truth shared with the plugin manifest. Falls back to
    ``"unknown"`` if the file is missing or malformed (e.g. when the script
    is invoked outside the plugin directory).
    """
    manifest = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
    try:
        with open(manifest) as f:
            data = json.load(f)
        version = data.get("version")
        if isinstance(version, str) and version:
            return version
    except (OSError, ValueError):
        pass
    return "unknown"


VERSION = _read_plugin_version()

# Identify ourselves on every outbound HTTP call. Python's default
# ``Python-urllib/X.Y`` User-Agent is blocked by Cloudflare's bot signature
# rules on logfire.pydantic.* (Error 1010), so we always send our own.
USER_AGENT = f"claude-code-logfire-plugin/{VERSION}"

# Refresh the access token when this many seconds (or fewer) remain before
# expiry. Matches the buffer used by ``scripts/oauth_intake_example.py`` in
# the platform repo.
REFRESH_BUFFER_SECONDS = 60

# Cap how long we wait for the cross-session refresh lock. Hooks have a hard
# timeout (10s for Stop/PreToolUse, 30s for SessionEnd), so we must give up
# well before that to leave time for the OTLP send itself.
LOCK_MAX_WAIT_SECONDS = 5.0
LOCK_POLL_INTERVAL = 0.1
LOCK_STALE_SECONDS = 30

TOKEN_DIR = Path.home() / ".logfire"
TOKEN_FILE = TOKEN_DIR / "claude-code-logfire-plugin.json"
LOCK_DIR = TOKEN_DIR / ".claude-code-logfire-plugin.lock"


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------


def _read_file() -> dict[str, Any] | None:
    """Return the stored bundle, or ``None`` if no usable bundle exists."""
    try:
        with open(TOKEN_FILE) as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    # Bundle must have at least a base_url + access_token to be useful.
    if not isinstance(data.get("base_url"), str) or not data.get("access_token"):
        return None
    return data


def _write_file(bundle: dict[str, Any]) -> None:
    """Atomically write ``bundle`` to ``TOKEN_FILE``. Raises ``OSError`` on
    failure so callers (notably ``cmd_login``) can surface the error rather
    than silently telling the user the token was saved."""
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="claude-code-logfire-plugin.", dir=str(TOKEN_DIR))
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(bundle, f, indent=2)
        os.replace(tmp, str(TOKEN_FILE))
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    # mkstemp already creates the file with mode 0600; the chmod is
    # belt-and-suspenders in case a non-POSIX platform widens it.
    with contextlib.suppress(OSError):
        os.chmod(str(TOKEN_FILE), 0o600)


# ---------------------------------------------------------------------------
# Cross-process lock (matches the pattern in log-event.py)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _lock():
    """Best-effort cross-process lock around the token store.

    Yields ``True`` if the lock was acquired, ``False`` if we timed out.
    Callers that mutate state should still proceed on ``False``
    (last-writer-wins beats dropping a freshly-issued token on the floor);
    callers that race refresh-token rotation should bail on ``False``.

    The release is gated on actual acquisition so a nested ``_lock()`` —
    e.g. a refresh path that re-enters via ``save_bundle`` — can't tear
    down its caller's lock.
    """
    acquired = False
    deadline = time.time() + LOCK_MAX_WAIT_SECONDS
    TOKEN_DIR.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            os.mkdir(LOCK_DIR)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - os.path.getmtime(LOCK_DIR) > LOCK_STALE_SECONDS:
                    with contextlib.suppress(OSError):
                        os.rmdir(LOCK_DIR)
                    continue
            except OSError:
                pass
            if time.time() >= deadline:
                break
            time.sleep(LOCK_POLL_INTERVAL)
    try:
        yield acquired
    finally:
        if acquired:
            with contextlib.suppress(OSError):
                os.rmdir(LOCK_DIR)


# ---------------------------------------------------------------------------
# Bundle accessors
# ---------------------------------------------------------------------------


def load_bundle() -> dict[str, Any] | None:
    """Return the stored bundle (with ``base_url`` field) or ``None``."""
    return _read_file()


def _save_bundle_unlocked(bundle: dict[str, Any]) -> None:
    """Write ``bundle`` without taking the lock. Callers must already hold
    ``_lock()`` (used by the refresh path, which can't re-enter the lock
    it already owns)."""
    if "base_url" not in bundle:
        raise ValueError("bundle is missing required 'base_url' field")
    _write_file(bundle)


def save_bundle(bundle: dict[str, Any]) -> None:
    with _lock():
        _save_bundle_unlocked(bundle)


def delete_bundle() -> None:
    with _lock(), contextlib.suppress(FileNotFoundError):
        os.unlink(TOKEN_FILE)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only)
# ---------------------------------------------------------------------------


def _http_get_json(url: str, timeout: float = 10.0) -> dict[str, Any]:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _http_post_form(url: str, fields: dict[str, Any], timeout: float = 30.0) -> dict[str, Any]:
    body = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def discover_metadata(base_url: str) -> dict[str, Any]:
    """RFC 8414 OAuth Authorization Server Metadata."""
    return _http_get_json(base_url.rstrip("/") + "/.well-known/oauth-authorization-server")


def discover_resource(base_url: str) -> str:
    """RFC 9728 protected-resource metadata — the JWT ``aud`` value the
    intake expects. Falls back to ``base_url`` if the endpoint is unavailable
    (e.g. self-hosted deployments without the metadata route)."""
    try:
        data = _http_get_json(base_url.rstrip("/") + "/.well-known/oauth-protected-resource/v1")
    except (urllib.error.URLError, OSError, ValueError):
        return base_url.rstrip("/")
    resource = data.get("resource")
    if isinstance(resource, str) and resource:
        return resource
    return base_url.rstrip("/")


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------


def _bundle_from_token_response(
    token: dict[str, Any],
    *,
    base_url: str,
    client_id: str,
    resource: str,
    token_endpoint: str = "",
    fallback_refresh: str = "",
    fallback_scope: str = "",
) -> dict[str, Any]:
    bundle: dict[str, Any] = {
        "base_url": base_url.rstrip("/"),
        "access_token": token["access_token"],
        "refresh_token": token.get("refresh_token") or fallback_refresh,
        "expires_at": time.time() + int(token.get("expires_in", 0)),
        "scope": token.get("scope") or fallback_scope,
        "client_id": client_id,
        "resource": resource,
    }
    if token_endpoint:
        bundle["token_endpoint"] = token_endpoint
    return bundle


def _refresh(bundle: dict[str, Any]) -> dict[str, Any]:
    """Exchange the refresh_token for a new access token. Raises on failure."""
    base_url: str = bundle["base_url"]
    # Prefer the cached endpoint to skip the discovery roundtrip on the hot
    # path; fall back to discovery for legacy bundles written before this
    # field existed (the new bundle we save below will include it).
    token_endpoint: str = bundle.get("token_endpoint") or ""
    if not token_endpoint:
        token_endpoint = discover_metadata(base_url)["token_endpoint"]
    resource: str = bundle.get("resource") or discover_resource(base_url)
    fields: dict[str, Any] = {
        "grant_type": "refresh_token",
        "refresh_token": bundle["refresh_token"],
        "client_id": bundle.get("client_id", ""),
        "scope": bundle.get("scope") or DEFAULT_SCOPE,
    }
    if resource:
        # RFC 8707 — required when the bundle was originally issued with
        # one, since the AS verifies the resource matches across refreshes.
        fields["resource"] = resource
    token = _http_post_form(token_endpoint, fields)
    new_bundle = _bundle_from_token_response(
        token,
        base_url=base_url,
        client_id=bundle.get("client_id", ""),
        resource=resource,
        token_endpoint=token_endpoint,
        fallback_refresh=bundle["refresh_token"],
        fallback_scope=bundle.get("scope", ""),
    )
    # We're called from inside ``_lock()`` (via ``get_access_token`` /
    # ``force_refresh``), so use the unlocked save to avoid the lock's
    # 5-second timeout waiting on ourselves.
    _save_bundle_unlocked(new_bundle)
    return new_bundle


def force_refresh() -> dict[str, Any]:
    """Force a refresh of the stored bundle and return it.

    Raises ``LookupError`` if there's no stored bundle (or no refresh token
    to exchange), ``RuntimeError`` if the cross-process lock can't be
    acquired, and the underlying ``urllib`` error if the AS rejects the
    refresh. Intended for the ``/logfire-session-capture:refresh`` slash
    command — ``get_access_token`` already refreshes lazily for the hook
    hot path.
    """
    with _lock() as acquired:
        if not acquired:
            raise RuntimeError("Could not acquire token-store lock")
        bundle = load_bundle()
        if not bundle:
            raise LookupError("No stored token; run `login` first")
        if not bundle.get("refresh_token"):
            raise LookupError("Stored token has no refresh_token; run `login` again")
        return _refresh(bundle)


def get_access_token(*, skip_refresh_if_valid: bool = False) -> tuple[str, str] | None:
    """Return ``(access_token, base_url)`` for the stored bundle, refreshing
    if needed.

    Returns ``None`` if no bundle is stored, or if the token has expired and
    cannot be refreshed. Designed to be called from the hook hot path so it
    must never raise.

    If ``skip_refresh_if_valid`` is set, return the stored access token as
    long as it has any validity left (i.e. ``expires_at > now``), without
    consulting the AS. Used by the SessionEnd hot path, which has a 30s
    budget and races a ``/exit``-triggered SIGTERM — a near-expiry refresh
    can blow that budget and leave the trace unclosed. The next session's
    SessionStart picks up the refresh.
    """
    bundle = load_bundle()
    if not bundle:
        return None
    base_url: str = bundle["base_url"]

    now = time.time()
    expires_at = float(bundle.get("expires_at", 0))
    if expires_at > now + REFRESH_BUFFER_SECONDS:
        token = bundle.get("access_token", "")
        return (token, base_url) if token else None

    if skip_refresh_if_valid and expires_at > now:
        token = bundle.get("access_token", "")
        return (token, base_url) if token else None

    if not bundle.get("refresh_token"):
        # No refresh capability — return what we have if it's still valid,
        # else give up.
        if expires_at > now:
            token = bundle.get("access_token", "")
            return (token, base_url) if token else None
        return None

    # Serialise refresh across sessions so refresh-token rotation doesn't
    # race itself into a 4xx.
    with _lock() as acquired:
        if not acquired:
            # Couldn't lock — fall back to the existing access token if it
            # still has any life in it.
            if expires_at > now:
                token = bundle.get("access_token", "")
                return (token, base_url) if token else None
            return None
        # Another session may have already refreshed while we waited.
        latest = load_bundle() or bundle
        latest_expires = float(latest.get("expires_at", 0))
        if latest_expires > now + REFRESH_BUFFER_SECONDS:
            token = latest.get("access_token", "")
            return (token, base_url) if token else None
        try:
            refreshed = _refresh(latest)
            token = refreshed.get("access_token", "")
            return (token, base_url) if token else None
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, KeyError):
            if latest_expires > now:
                token = latest.get("access_token", "")
                return (token, base_url) if token else None
            return None
