"""WebSocket relay for the remote mixer surface (design spec §6).

One implicit room per deployment: every authenticated client connects to
``/api/remote/ws`` and each JSON text frame is fanned out verbatim to every
*other* connected client. The relay holds no mix state and interprets
nothing — the playing browser publishes ``state`` snapshots, remote surfaces
send ``mix``/``mute``/``solo``/``master`` commands, and each side ignores
frame types it does not understand.

Auth: browsers cannot set headers on WebSocket upgrades, so the device token
rides a ``token`` query parameter, verified with the same HMAC as the REST
surface. The socket is accepted first and closed with application code 4401
on failure so clients can distinguish auth rejection from network loss.
Session-scoped WS credentials remain a documented follow-up (see auth.py).
"""

from __future__ import annotations

import asyncio
import contextlib

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .auth import verify_device_token

# Commands are tiny JSON frames; anything larger is not ours to relay.
MAX_FRAME_CHARS = 4096

router = APIRouter(prefix="/api")


class RemoteHub:
    """Connected sockets for the single deployment-wide remote room."""

    def __init__(self) -> None:
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def join(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.add(ws)

    async def leave(self, ws: WebSocket) -> None:
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, sender: WebSocket, text: str) -> None:
        async with self._lock:
            targets = [c for c in self._clients if c is not sender]
        for client in targets:
            # A dead peer is reaped by its own handler; never let one broken
            # socket stop the fan-out to the rest.
            with contextlib.suppress(Exception):
                await client.send_text(text)


def get_hub(ws: WebSocket) -> RemoteHub:
    hub = getattr(ws.app.state, "remote_hub", None)
    if hub is None:
        hub = ws.app.state.remote_hub = RemoteHub()
    return hub


@router.websocket("/remote/ws")
async def remote_ws(ws: WebSocket) -> None:
    settings = ws.app.state.settings
    await ws.accept()
    if settings.auth_enabled and not verify_device_token(
        settings, ws.query_params.get("token")
    ):
        await ws.close(code=4401, reason="Authentication required")
        return

    hub = get_hub(ws)
    await hub.join(ws)
    try:
        while True:
            text = await ws.receive_text()
            if len(text) <= MAX_FRAME_CHARS:
                await hub.broadcast(ws, text)
    except WebSocketDisconnect:
        pass
    finally:
        await hub.leave(ws)
