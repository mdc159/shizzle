"""HTTP client for the node routes of the control plane (library/.../api/nodes.py)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

import httpx


class ApiError(RuntimeError):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Assignment:
    id: uuid.UUID
    job_id: str
    kind: str
    status: str
    idempotency_key: str | None
    payload: dict[str, Any]

    @classmethod
    def from_json(cls, body: dict[str, Any]) -> Assignment:
        return cls(
            id=uuid.UUID(str(body["assignmentId"])),
            job_id=str(body["jobId"]),
            kind=str(body["kind"]),
            status=str(body["status"]),
            idempotency_key=body.get("idempotencyKey"),
            payload=dict(body.get("payload") or {}),
        )


class NodeClient:
    def __init__(
        self,
        api_base: str,
        node_id: str,
        token: str,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 60.0,
    ) -> None:
        self._base = f"{api_base.rstrip('/')}/api/nodes/{node_id}"
        self._http = httpx.Client(
            headers={"Authorization": f"Bearer {token}", "User-Agent": "shizzle-node"},
            transport=transport,
            timeout=timeout,
        )

    def close(self) -> None:
        self._http.close()

    def _check(self, response: httpx.Response) -> dict[str, Any]:
        if response.status_code >= 400:
            try:
                detail = str(response.json().get("detail", response.text))
            except ValueError:
                detail = response.text[:300]
            raise ApiError(response.status_code, detail)
        if response.status_code == 204 or not response.content:
            return {}
        data = response.json()
        return data if isinstance(data, dict) else {}

    def heartbeat(
        self,
        *,
        display_name: str,
        capabilities: dict[str, Any],
        agent_version: str,
        busy_assignment_id: uuid.UUID | None,
    ) -> None:
        self._check(
            self._http.post(
                f"{self._base}/heartbeat",
                json={
                    "displayName": display_name,
                    "capabilities": capabilities,
                    "agentVersion": agent_version,
                    "busyAssignmentId": str(busy_assignment_id) if busy_assignment_id else None,
                },
            )
        )

    def next_assignment(self, *, wait_seconds: int) -> Assignment | None:
        response = self._http.get(
            f"{self._base}/assignments/next",
            params={"wait": wait_seconds},
            timeout=wait_seconds + 30,
        )
        body = self._check(response)
        return Assignment.from_json(body) if body else None

    def ack(self, assignment_id: uuid.UUID) -> None:
        self._check(self._http.post(f"{self._base}/assignments/{assignment_id}/ack"))

    def progress(self, assignment_id: uuid.UUID, phase: str) -> None:
        self._check(
            self._http.post(
                f"{self._base}/assignments/{assignment_id}/progress", json={"phase": phase[:256]}
            )
        )

    def complete(self, assignment_id: uuid.UUID, detail: dict[str, Any]) -> None:
        self._check(
            self._http.post(
                f"{self._base}/assignments/{assignment_id}/complete", json={"detail": detail}
            )
        )

    def fail(self, assignment_id: uuid.UUID, *, code: str, detail: str) -> None:
        self._check(
            self._http.post(
                f"{self._base}/assignments/{assignment_id}/fail",
                json={"code": code[:64], "detail": detail[:2000]},
            )
        )
