"""Shared recursive redaction for public job and administrator responses.

Free-text diagnostics can echo submitted URLs, nested credential-shaped JSON,
or provider secrets. Keep one policy so adding a response field cannot create a
second, weaker redaction rule.
"""

from __future__ import annotations

import re
from typing import Any

_REDACTED = "[redacted]"
_REDACT_KEY_FRAGMENTS = (
    "source_ref",
    "authorization",
    "cookie",
    "password",
    "passcode",
    "token",
    "secret",
    "url",
    "access_key",
    "access-key",
    "accesskey",
    "api_key",
    "api-key",
    "apikey",
)
_URL_SCRUB_RE = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)


def _redact_value(value: Any) -> Any:
    """Omit credential/source keys recursively and scrub URLs from strings."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            original_key = str(key)
            lowered = original_key.lower()
            base_key = _URL_SCRUB_RE.sub(_REDACTED, original_key)
            # Two credential-bearing keys can scrub to the same placeholder.
            # Keep both entries without reintroducing their sensitive text.
            redacted_key = base_key
            suffix = 2
            while redacted_key in redacted:
                redacted_key = f"{base_key} ({suffix})"
                suffix += 1
            if any(fragment in lowered for fragment in _REDACT_KEY_FRAGMENTS):
                # The key name itself is noise (notably source_ref); omit the
                # whole field rather than returning a placeholder value.
                continue
            redacted[redacted_key] = _redact_value(child)
        return redacted
    if isinstance(value, list):
        return [_redact_value(child) for child in value]
    if isinstance(value, str):
        return _URL_SCRUB_RE.sub(_REDACTED, value)
    return value
