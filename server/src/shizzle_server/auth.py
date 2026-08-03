"""Shared-passcode auth (design spec §7), launch-minimal.

One passcode gates the API. A correct passcode mints an opaque device token:
an HMAC over ``auth_version : passcode : expiry`` keyed by
``TOKEN_SIGNING_SECRET``. Binding the passcode into the signature means a
passcode rotation invalidates every issued token for free (the review's
"passcode rotation revokes all tokens" property, without a token store).

The gate is OFF when no passcode is configured — the local single-container
profile and the unit suite run open. On the VPS a real ``SHIZZLE_PASSCODE``
turns it on.

Not yet built (documented gaps, not launch blockers): per-device revocation
lists, QR/session-scoped WebSocket credentials (§6 remote surface).
"""

from __future__ import annotations

import base64
import hmac
import time
from hashlib import sha256

from fastapi import HTTPException, Request

from .settings import Settings

TOKEN_COOKIE = "shizzle_token"


def _sign(settings: Settings, expiry: int) -> str:
    msg = f"{settings.auth_version}:{settings.shizzle_passcode}:{expiry}".encode()
    sig = hmac.new(settings.token_signing_secret.encode(), msg, sha256).digest()
    return base64.urlsafe_b64encode(sig).decode().rstrip("=")


def create_device_token(settings: Settings, ttl_seconds: int | None = None) -> tuple[str, int]:
    """Return ``(token, expiry_epoch)``. Token is ``<expiry>.<sig>``."""
    ttl = ttl_seconds if ttl_seconds is not None else settings.auth_token_ttl_seconds
    expiry = int(time.time()) + ttl
    return f"{expiry}.{_sign(settings, expiry)}", expiry


def verify_device_token(settings: Settings, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expiry_str, sig = token.split(".", 1)
    try:
        expiry = int(expiry_str)
    except ValueError:
        return False
    if expiry < int(time.time()):
        return False
    return hmac.compare_digest(sig, _sign(settings, expiry))


def check_passcode(settings: Settings, passcode: str) -> bool:
    if not settings.shizzle_passcode:
        return False
    return hmac.compare_digest(passcode.encode(), settings.shizzle_passcode.encode())


def _token_from_request(request: Request) -> str | None:
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.cookies.get(TOKEN_COOKIE)


def require_auth(request: Request) -> None:
    """FastAPI dependency. No-op when the gate is disabled; else 401 on a bad token."""
    settings: Settings = request.app.state.settings
    if not settings.auth_enabled:
        return
    if not verify_device_token(settings, _token_from_request(request)):
        raise HTTPException(status_code=401, detail="Authentication required")
