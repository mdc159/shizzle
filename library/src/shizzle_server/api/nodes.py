"""Worker-node API (docs/WORKER-NODES.md).

Nodes pull: they heartbeat, long-poll for an assignment, acknowledge it,
report progress, and report completion or failure. Every route here requires
the node's own bearer token from ``SHIZZLE_NODE_TOKENS``; the shared passcode
never authenticates a node and a node token authenticates nothing else (E6).
The dashboard's read-only listing is the one passcode-gated route.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, field_validator

from ..db.models import Node, NodeAssignment
from ..db.repository import NodeRepository
from ..settings import Settings
from .auth import require_auth

router = APIRouter(prefix="/api/nodes")

_LONG_POLL_STEP_SECONDS = 1.0
_LONG_POLL_MAX_SECONDS = 30


# --- schemas -------------------------------------------------------------------


class NodeHeartbeatRequest(BaseModel):
    displayName: str | None = Field(None, max_length=128)
    # Omitted keeps the stored capability set; only an explicit dict replaces it.
    capabilities: dict[str, Any] | None = None
    agentVersion: str | None = Field(None, max_length=64)
    busyAssignmentId: uuid.UUID | None = None


class NodeHeartbeatResponse(BaseModel):
    ok: bool = True
    serverTime: datetime


class AssignmentResponse(BaseModel):
    assignmentId: uuid.UUID
    jobId: str
    kind: str
    status: str
    idempotencyKey: str | None = None
    payload: dict[str, Any]
    createdAt: datetime

    @classmethod
    def from_assignment(cls, assignment: NodeAssignment) -> AssignmentResponse:
        return cls(
            assignmentId=assignment.id,
            jobId=assignment.job_id.hex,
            kind=assignment.kind.value,
            status=assignment.status.value,
            idempotencyKey=assignment.idempotency_key,
            payload=dict(assignment.payload or {}),
            createdAt=assignment.created_at,
        )


class ProgressRequest(BaseModel):
    phase: str = Field(min_length=1, max_length=256)


_COMPLETE_DETAIL_MAX_BYTES = 8192


class CompleteRequest(BaseModel):
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("detail")
    @classmethod
    def _bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        # A completion report is a hint (the S3 markers are the proof), so it
        # is kept small: the documented summary fields fit in well under 8 KiB.
        if len(json.dumps(value, separators=(",", ":"))) > _COMPLETE_DETAIL_MAX_BYTES:
            raise ValueError(f"detail must serialize to at most {_COMPLETE_DETAIL_MAX_BYTES} bytes")
        return value


class FailRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    detail: str = Field(default="", max_length=2000)


class NodeInfo(BaseModel):
    id: str
    displayName: str
    online: bool
    disabled: bool
    capabilities: dict[str, Any]
    agentVersion: str | None = None
    lastSeenAt: datetime | None = None
    busyAssignmentId: uuid.UUID | None = None

    @classmethod
    def from_node(cls, node: Node, *, online_seconds: float, now: datetime) -> NodeInfo:
        seen = node.last_seen_at
        if seen is not None and seen.tzinfo is None:
            seen = seen.replace(tzinfo=UTC)
        online = seen is not None and (now - seen).total_seconds() <= online_seconds
        return cls(
            id=node.id,
            displayName=node.display_name or node.id,
            online=online and node.disabled_at is None,
            disabled=node.disabled_at is not None,
            capabilities=dict(node.capabilities or {}),
            agentVersion=node.agent_version,
            lastSeenAt=seen,
            busyAssignmentId=node.busy_assignment_id,
        )


class NodeListResponse(BaseModel):
    nodes: list[NodeInfo]
    onlineSeconds: float


# --- auth ----------------------------------------------------------------------


def _settings(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


def _nodes(request: Request) -> NodeRepository:
    repo: NodeRepository = request.app.state.node_repo
    return repo


def require_node(request: Request, node_id: str) -> str:
    """FastAPI dependency: the bearer token must be this node's configured token.

    Unknown node, missing token and wrong token all answer 401 so the route
    is not an oracle for which node ids exist.
    """
    expected = _settings(request).node_tokens.get(node_id)
    auth = request.headers.get("authorization", "")
    presented = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
    # Bytes, not str: compare_digest raises TypeError on non-ASCII str, which
    # would turn a bad token into a 500 instead of a 401.
    if (
        not expected
        or not presented
        or not hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(status_code=401, detail="Node authentication required")
    return node_id


NodeAuth = Depends(require_node)


def _parse_assignment_id(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError:
        raise HTTPException(404, "Assignment not found") from None


# --- dashboard listing (passcode) --------------------------------------------


@router.get("")
async def list_nodes(request: Request, _auth: None = Depends(require_auth)) -> NodeListResponse:
    settings = _settings(request)
    now = datetime.now(UTC)
    nodes = await _nodes(request).list_nodes()
    return NodeListResponse(
        nodes=[
            NodeInfo.from_node(node, online_seconds=settings.node_online_seconds, now=now)
            for node in nodes
        ],
        onlineSeconds=settings.node_online_seconds,
    )


# --- node routes (node token) --------------------------------------------------


@router.post("/{node_id}/heartbeat")
async def heartbeat(
    request: Request, body: NodeHeartbeatRequest, node_id: str = NodeAuth
) -> NodeHeartbeatResponse:
    await _nodes(request).heartbeat(
        node_id,
        display_name=body.displayName,
        capabilities=body.capabilities,
        agent_version=body.agentVersion,
        busy_assignment_id=body.busyAssignmentId,
    )
    return NodeHeartbeatResponse(serverTime=datetime.now(UTC))


@router.get("/{node_id}/assignments/next")
async def next_assignment(
    request: Request,
    response: Response,
    node_id: str = NodeAuth,
    wait: int = Query(default=0, ge=0, le=_LONG_POLL_MAX_SECONDS),
) -> AssignmentResponse | None:
    """Long-poll: the oldest open assignment for this node, or 204 after ``wait`` seconds."""
    repo = _nodes(request)
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        assignment = await repo.next_for_node(node_id)
        if assignment is not None:
            return AssignmentResponse.from_assignment(assignment)
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0 or await request.is_disconnected():
            response.status_code = 204
            return None
        await asyncio.sleep(min(_LONG_POLL_STEP_SECONDS, remaining))


@router.post("/{node_id}/assignments/{assignment_id}/ack")
async def acknowledge(request: Request, assignment_id: str, node_id: str = NodeAuth) -> dict[str, bool]:
    ok = await _nodes(request).acknowledge(_parse_assignment_id(assignment_id), node_id=node_id)
    if not ok:
        raise HTTPException(409, "Assignment is not open for this node")
    return {"ok": True}


@router.post("/{node_id}/assignments/{assignment_id}/progress")
async def progress(
    request: Request, assignment_id: str, body: ProgressRequest, node_id: str = NodeAuth
) -> dict[str, bool]:
    ok = await _nodes(request).progress(
        _parse_assignment_id(assignment_id), node_id=node_id, phase=body.phase
    )
    if not ok:
        raise HTTPException(409, "Assignment is not open for this node")
    return {"ok": True}


@router.post("/{node_id}/assignments/{assignment_id}/complete")
async def complete(
    request: Request, assignment_id: str, body: CompleteRequest, node_id: str = NodeAuth
) -> dict[str, bool]:
    ok = await _nodes(request).complete(
        _parse_assignment_id(assignment_id), node_id=node_id, detail=body.detail
    )
    if not ok:
        raise HTTPException(409, "Assignment is not open for this node")
    return {"ok": True}


@router.post("/{node_id}/assignments/{assignment_id}/fail")
async def fail(
    request: Request, assignment_id: str, body: FailRequest, node_id: str = NodeAuth
) -> dict[str, bool]:
    ok = await _nodes(request).fail(
        _parse_assignment_id(assignment_id),
        node_id=node_id,
        detail={"code": body.code, "detail": body.detail[:2000]},
    )
    if not ok:
        raise HTTPException(409, "Assignment is not open for this node")
    return {"ok": True}
