"""Unit: passcode gate, device tokens, CloudFront signing, manifest rewrite."""

from __future__ import annotations

import base64

import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from shizzle_server import cloudfront, media
from shizzle_server.auth import (
    check_passcode,
    create_device_token,
    verify_device_token,
)
from shizzle_server.main import create_app
from shizzle_server.settings import Settings


def _s(**kw) -> Settings:
    base = {
        "database_url": "sqlite+aiosqlite:///:memory:",
        "shizzle_embedded_orchestrator": False,
    }
    base.update(kw)
    return Settings(**base)


# --- tokens ------------------------------------------------------------------


def test_token_round_trip_and_expiry():
    s = _s(shizzle_passcode="hunter2", token_signing_secret="k")
    token, _ = create_device_token(s)
    assert verify_device_token(s, token)
    # Tamper -> reject
    assert not verify_device_token(s, token[:-1] + ("x" if token[-1] != "x" else "y"))
    # Expired
    expired, _ = create_device_token(s, ttl_seconds=-10)
    assert not verify_device_token(s, expired)


def test_passcode_rotation_revokes_tokens():
    s1 = _s(shizzle_passcode="old", token_signing_secret="k")
    token, _ = create_device_token(s1)
    assert verify_device_token(s1, token)
    s2 = _s(shizzle_passcode="new", token_signing_secret="k")
    # Passcode is bound into the signature: old token no longer verifies.
    assert not verify_device_token(s2, token)


def test_check_passcode():
    s = _s(shizzle_passcode="abc")
    assert check_passcode(s, "abc")
    assert not check_passcode(s, "xyz")
    assert not check_passcode(_s(), "anything")  # gate disabled -> no valid passcode


# --- CloudFront signing (shape; matches spike 0.2 cookie contract) -----------


def test_signed_cookies_shape(tmp_path):
    # Minimal RSA key so signing runs end to end.
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    key_path = tmp_path / "cf.pem"
    key_path.write_bytes(pem)

    s = _s(
        cloudfront_domain="d2488k8kjndpsy.cloudfront.net",
        cloudfront_key_pair_id="KRNC9VLVC15DN",
        cloudfront_private_key_path=str(key_path),
    )
    assert s.cloudfront_enabled
    cookies = cloudfront.signed_cookies(s, expires_epoch=9999999999)
    assert set(cookies) == {
        "CloudFront-Policy",
        "CloudFront-Signature",
        "CloudFront-Key-Pair-Id",
    }
    assert cookies["CloudFront-Key-Pair-Id"] == "KRNC9VLVC15DN"
    # CloudFront base64 variant uses -, _, ~ (never +, =, /).
    for token in (cookies["CloudFront-Policy"], cookies["CloudFront-Signature"]):
        assert not (set(token) & set("+=/"))
    # Policy decodes to JSON referencing the /tracks/* glob.
    restored = cookies["CloudFront-Policy"].replace("-", "+").replace("_", "=").replace("~", "/")
    assert b"/tracks/*" in base64.b64decode(restored)


# --- manifest rewrite --------------------------------------------------------


def test_rewrite_cloud_manifest_to_cdn_paths():
    s = _s(media_cookie_path="/cdn")
    raw = {
        "title": "Song",
        "video": "video.mp4",
        "stems": [
            {"id": "vocals", "file": "stems/vocals.m4a", "default_gain_db": 0.0},
            {"id": "drums", "file": "stems/drums.m4a", "default_gain_db": 0.0},
        ],
    }
    out = media.rewrite_cloud_manifest(s, raw, "tracks/abc/1")
    assert out["video"] == "/cdn/tracks/abc/1/video.mp4"
    assert out["stems"][0]["file"] == "/cdn/tracks/abc/1/stems/vocals.m4a"
    # Original untouched.
    assert raw["video"] == "video.mp4"


# --- gate behaviour through the ASGI app -------------------------------------


@pytest_asyncio.fixture
async def gated_client(tmp_path):
    s = _s(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 't.db'}",
        data_dir=tmp_path / "data",
        shizzle_passcode="letmein",
        shizzle_pipeline="test",
    )
    (tmp_path / "data").mkdir()
    app = create_app(s)
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c


async def test_gate_blocks_then_allows(gated_client):
    c = gated_client
    # Health is always open.
    assert (await c.get("/api/health")).status_code == 200
    # Library is gated.
    assert (await c.get("/api/library")).status_code == 401
    # Wrong passcode.
    assert (await c.post("/api/auth", json={"passcode": "nope"})).status_code == 401
    # Correct passcode mints a token.
    ok = await c.post("/api/auth", json={"passcode": "letmein"})
    assert ok.status_code == 200
    token = ok.json()["token"]
    # Bearer token opens the gate.
    lib = await c.get("/api/library", headers={"Authorization": f"Bearer {token}"})
    assert lib.status_code == 200
