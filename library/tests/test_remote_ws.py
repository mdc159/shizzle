"""Unit: /api/remote/ws relay — fan-out, no echo, auth gate, disconnect cleanup."""

from __future__ import annotations

import asyncio

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from shizzle_server.api import remote
from shizzle_server.api.auth import TOKEN_COOKIE, create_device_token
from shizzle_server.main import create_app


def test_relay_fans_out_to_other_clients(settings):
    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/api/remote/ws") as a,
        client.websocket_connect("/api/remote/ws") as b,
        client.websocket_connect("/api/remote/ws") as c,
    ):
        a.send_json({"type": "mix", "stem": "vocals", "gainDb": -12.5})
        assert b.receive_json() == {"type": "mix", "stem": "vocals", "gainDb": -12.5}
        assert c.receive_json() == {"type": "mix", "stem": "vocals", "gainDb": -12.5}


def test_relay_does_not_echo_to_sender(settings):
    with (
        TestClient(create_app(settings)) as client,
        client.websocket_connect("/api/remote/ws") as a,
        client.websocket_connect("/api/remote/ws") as b,
    ):
        a.send_json({"type": "mute", "stem": "drums", "on": True})
        b.send_json({"type": "master", "value": 0.5})
        # a's first inbound frame is b's message — its own was not echoed.
        assert a.receive_json() == {"type": "master", "value": 0.5}


def test_survives_peer_disconnect(settings):
    with TestClient(create_app(settings)) as client:
        a_cm = client.websocket_connect("/api/remote/ws")
        b_cm = client.websocket_connect("/api/remote/ws")
        a = a_cm.__enter__()
        b_cm.__enter__()
        b_cm.__exit__(None, None, None)  # b leaves
        with client.websocket_connect("/api/remote/ws") as c:
            a.send_json({"type": "solo", "stem": "piano", "on": True})
            assert c.receive_json() == {"type": "solo", "stem": "piano", "on": True}
        a_cm.__exit__(None, None, None)


def test_auth_rejects_missing_or_bad_token(settings):
    secured = settings.model_copy(
        update={"shizzle_passcode": "letmein", "token_signing_secret": "unit-secret"}
    )
    with TestClient(create_app(secured)) as client:
        with client.websocket_connect("/api/remote/ws") as ws:
            try:
                ws.receive_json()
                raise AssertionError("unauthenticated socket was not closed")
            except WebSocketDisconnect as exc:
                assert exc.code == 4401

        with client.websocket_connect("/api/remote/ws?token=bogus.sig") as ws:
            try:
                ws.receive_json()
                raise AssertionError("bad-token socket was not closed")
            except WebSocketDisconnect as exc:
                assert exc.code == 4401


def test_auth_accepts_valid_token(settings):
    secured = settings.model_copy(
        update={"shizzle_passcode": "letmein", "token_signing_secret": "unit-secret"}
    )
    token, _ = create_device_token(secured)
    with (
        TestClient(create_app(secured)) as client,
        client.websocket_connect(
            "/api/remote/ws", headers={"cookie": f"{TOKEN_COOKIE}={token}"}
        ) as a,
        client.websocket_connect(
            "/api/remote/ws", headers={"cookie": f"{TOKEN_COOKIE}={token}"}
        ) as b,
    ):
        a.send_json({"type": "mix", "stem": "bass", "gainDb": 3.0})
        assert b.receive_json() == {"type": "mix", "stem": "bass", "gainDb": 3.0}


@pytest.mark.asyncio
async def test_slow_peer_does_not_block_healthy_peer(monkeypatch):
    class Peer:
        def __init__(self, blocked: bool = False) -> None:
            self.blocked = blocked
            self.closed = False
            self.messages: list[str] = []

        async def send_text(self, text: str) -> None:
            if self.blocked:
                await asyncio.Event().wait()
            self.messages.append(text)

        async def close(self, **_kwargs) -> None:
            self.closed = True

    monkeypatch.setattr(remote, "SEND_TIMEOUT_SECONDS", 0.01)
    hub = remote.RemoteHub()
    sender = Peer()
    stuck = Peer(blocked=True)
    healthy = Peer()
    await hub.join(sender)  # type: ignore[arg-type]
    await hub.join(stuck)  # type: ignore[arg-type]
    await hub.join(healthy)  # type: ignore[arg-type]

    await hub.broadcast(sender, '{"type":"master","value":0.5}')  # type: ignore[arg-type]

    assert healthy.messages == ['{"type":"master","value":0.5}']
    assert stuck.messages == []
    assert stuck.closed

    await hub.broadcast(sender, '{"type":"master","value":0.6}')  # type: ignore[arg-type]
    assert healthy.messages == [
        '{"type":"master","value":0.5}',
        '{"type":"master","value":0.6}',
    ]
