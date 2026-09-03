"""RunPod serverless client with structured errors and circuit breaking."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import httpx

from ..errors import ErrorCode, StageError
from ..lib.circuit_breaker import CircuitBreaker

logger = logging.getLogger(__name__)

_KNOWN_STATUSES = frozenset(
    {"IN_QUEUE", "IN_PROGRESS", "COMPLETED", "FAILED", "CANCELLED", "TIMED_OUT"}
)


class RunPodClient(Protocol):
    """Submit, inspect, and cancel RunPod serverless jobs."""

    async def dispatch(
        self, *, job_id: uuid.UUID, idempotency_key: str, payload: dict[str, Any]
    ) -> str:
        """Submit a job and return its RunPod job id."""
        ...

    async def poll(self, runpod_job_id: str) -> dict[str, Any]:
        """Fetch the current RunPod status payload."""
        ...

    async def cancel(self, runpod_job_id: str) -> None:
        """Cancel a RunPod job."""
        ...


class NotConfiguredRunPodClient:
    """Parked-cloud stand-in used while cloud mode lacks RunPod settings.

    The orchestrator starts in the valid parked-cloud state when
    SHIZZLE_PIPELINE=cloud has no RUNPOD_API_KEY / RUNPOD_ENDPOINT_ID — it
    logs a warning and keeps its heartbeat green — with this client in place
    until configuration arrives. Fresh dispatches fail closed with a
    non-retryable RUNPOD_DISPATCH_FAILED (a job that cannot start is
    terminally misconfigured). Polling an already-dispatched remote job
    raises the same code but retryable: the failure says nothing about the
    remote job, so the dispatched handler parks it and it reconciles on the
    first poll after credentials return.
    """

    _DETAIL = (
        "RunPod is not configured: SHIZZLE_PIPELINE=cloud requires "
        "RUNPOD_API_KEY and RUNPOD_ENDPOINT_ID"
    )

    async def dispatch(
        self, *, job_id: uuid.UUID, idempotency_key: str, payload: dict[str, Any]
    ) -> str:
        del job_id, idempotency_key, payload
        raise StageError(ErrorCode.RUNPOD_DISPATCH_FAILED, self._DETAIL, retryable=False)

    async def poll(self, runpod_job_id: str) -> dict[str, Any]:
        del runpod_job_id
        raise StageError(ErrorCode.RUNPOD_DISPATCH_FAILED, self._DETAIL, retryable=True)

    async def cancel(self, runpod_job_id: str) -> None:
        del runpod_job_id
        raise StageError(ErrorCode.RUNPOD_DISPATCH_FAILED, self._DETAIL, retryable=False)


class HttpRunPodClient:
    """HTTP implementation of the RunPod serverless API."""

    def __init__(
        self,
        api_key: str,
        endpoint_id: str,
        api_base: str = "https://api.runpod.ai/v2",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._endpoint_url = f"{api_base.rstrip('/')}/{endpoint_id}"
        self._headers = {"Authorization": f"Bearer {api_key}"}
        self._transport = transport
        self._breaker: CircuitBreaker[httpx.Response] = CircuitBreaker(
            failure_threshold=5, timeout_seconds=60, name="runpod"
        )

    async def dispatch(
        self, *, job_id: uuid.UUID, idempotency_key: str, payload: dict[str, Any]
    ) -> str:
        # RunPod exposes no idempotency header; this key is trace context only.
        logger.info("job %s: dispatching to RunPod (trace=%s)", job_id, idempotency_key)
        response = await self._call(
            lambda: self._request("POST", "/run", json={"input": payload})
        )
        try:
            runpod_job_id = response.json()["id"]
        except (KeyError, TypeError, ValueError) as exc:
            raise StageError(
                ErrorCode.RUNPOD_DISPATCH_FAILED,
                "RunPod dispatch response did not contain a job id",
            ) from exc
        if not isinstance(runpod_job_id, str) or not runpod_job_id:
            raise StageError(
                ErrorCode.RUNPOD_DISPATCH_FAILED,
                "RunPod dispatch response did not contain a job id",
            )
        return runpod_job_id

    async def poll(self, runpod_job_id: str) -> dict[str, Any]:
        response = await self._call(
            lambda: self._request("GET", f"/status/{runpod_job_id}")
        )
        try:
            payload = response.json()
        except ValueError as exc:
            raise StageError(
                ErrorCode.RUNPOD_DISPATCH_FAILED, "RunPod status response was not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise StageError(
                ErrorCode.RUNPOD_DISPATCH_FAILED, "RunPod status response was not an object"
            )
        return payload

    async def cancel(self, runpod_job_id: str) -> None:
        await self._call(lambda: self._request("POST", f"/cancel/{runpod_job_id}"))

    async def _call(self, factory: Callable[[], Awaitable[httpx.Response]]) -> httpx.Response:
        try:
            response = await self._breaker.call_async(factory)
        except RuntimeError as exc:
            raise StageError(
                ErrorCode.RUNPOD_DISPATCH_FAILED,
                "RunPod circuit breaker is open",
                retryable=True,
            ) from exc
        if response.status_code >= 400:
            raise StageError(
                ErrorCode.RUNPOD_DISPATCH_FAILED,
                f"RunPod returned HTTP {response.status_code}",
                retryable=False,
            )
        return response

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            async with httpx.AsyncClient(
                headers=self._headers, transport=self._transport, timeout=30
            ) as client:
                response = await client.request(method, f"{self._endpoint_url}{path}", **kwargs)
        except httpx.TimeoutException as exc:
            raise StageError(ErrorCode.RUNPOD_TIMEOUT, str(exc), retryable=True) from exc
        except httpx.RequestError as exc:
            raise StageError(
                ErrorCode.RUNPOD_DISPATCH_FAILED, str(exc), retryable=True
            ) from exc

        if response.status_code < 400 or (
            400 <= response.status_code < 500 and response.status_code != 429
        ):
            return response
        raise StageError(
            ErrorCode.RUNPOD_DISPATCH_FAILED,
            f"RunPod returned HTTP {response.status_code}",
            retryable=True,
        )


def _latest_phase(value: Any) -> str | None:
    if isinstance(value, str):
        return value or None
    if isinstance(value, dict):
        for key in ("phase", "message", "progress", "status"):
            phase = _latest_phase(value.get(key))
            if phase is not None:
                return phase
        for nested in reversed(list(value.values())):
            phase = _latest_phase(nested)
            if phase is not None:
                return phase
    elif isinstance(value, list):
        for nested in reversed(value):
            phase = _latest_phase(nested)
            if phase is not None:
                return phase
    return None


def parse_worker_progress(payload: dict[str, Any]) -> tuple[str, str | None]:
    """Return normalized RunPod status and the latest worker progress string."""
    raw_status = payload.get("status")
    status = raw_status.upper() if isinstance(raw_status, str) else ""
    if status not in _KNOWN_STATUSES:
        logger.warning("unknown RunPod status %r; treating it as IN_PROGRESS", raw_status)
        status = "IN_PROGRESS"
    return status, _latest_phase(payload.get("output"))
