"""CloudFront signed cookies for private-S3 media delivery (design spec §3/§7).

Lifted from the proven spike (``spikes/signed-cookie-proof/prove.py``, spike
0.2 — 5/5 checks: 403 no-cookie / 200 signed / 206 Range / 403 expired). The
signing is purely local (no AWS calls at request time): a custom policy over a
resource glob, RSA-SHA1 PKCS#1 v1.5, base64 with CloudFront's URL-safe
substitutions, delivered as the three ``CloudFront-*`` cookies.

Topology note: the media is fronted by CloudFront (OAC + trusted key group) but
reached same-origin via Caddy's ``/cdn`` reverse proxy, so these cookies are
set on the app origin (shizzle.systems) and ride along on same-origin media
requests. The policy Resource is the CloudFront URL the edge actually sees.
"""

from __future__ import annotations

import base64
import json
import time
from functools import lru_cache
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

from .settings import Settings

POLICY_COOKIE = "CloudFront-Policy"
SIGNATURE_COOKIE = "CloudFront-Signature"
KEY_PAIR_COOKIE = "CloudFront-Key-Pair-Id"


def _cf_b64(data: bytes) -> str:
    """Base64 with CloudFront's substitutions (+ -> -, = -> _, / -> ~)."""
    return base64.b64encode(data).decode("ascii").replace("+", "-").replace("=", "_").replace("/", "~")


def _build_policy(resource: str, expires_epoch: int) -> str:
    """Compact custom-policy JSON. Whitespace matters — padded JSON breaks the signature."""
    policy = {
        "Statement": [
            {
                "Resource": resource,
                "Condition": {"DateLessThan": {"AWS:EpochTime": expires_epoch}},
            }
        ]
    }
    return json.dumps(policy, separators=(",", ":"))


@lru_cache(maxsize=4)
def _load_key(path: str) -> RSAPrivateKey:
    key = serialization.load_pem_private_key(Path(path).read_bytes(), password=None)
    if not isinstance(key, RSAPrivateKey):
        raise TypeError("CloudFront signing key must be RSA")
    return key


def signed_cookies(settings: Settings, expires_epoch: int | None = None) -> dict[str, str]:
    """The three CloudFront cookie values for a custom policy over the media glob."""
    if expires_epoch is None:
        expires_epoch = int(time.time()) + settings.media_ttl_seconds
    resource = f"{settings.cloudfront_resource_base}/tracks/*"
    policy = _build_policy(resource, expires_epoch)
    key = _load_key(settings.cloudfront_private_key_path)
    signature = key.sign(policy.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())
    return {
        POLICY_COOKIE: _cf_b64(policy.encode("utf-8")),
        SIGNATURE_COOKIE: _cf_b64(signature),
        KEY_PAIR_COOKIE: settings.cloudfront_key_pair_id,
    }
