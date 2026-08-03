"""Phase 3 seam: RunPod serverless client interface.

Phase 2 ships the *interface only* so the dispatched/splitting cloud handlers
have a stable seam to build against. No fake implementations: the stub raises,
and `dispatched` is not in RUNNABLE_STAGES, so the Phase 2 loop never claims it.

TODO(Phase 3, 3.2):
  - Implement dispatch() against the RunPod serverless endpoint (idempotency
    key on the request; endpoint pinned to 1 worker / 1 concurrent job).
  - Webhook receiver (fast path) + polling reconciler (recovery path — RunPod
    retains async results only ~30 min and retries webhooks only twice).
  - Wrap calls in shizzle_server.lib.circuit_breaker.CircuitBreaker.
  - Map failures to ErrorCode.RUNPOD_DISPATCH_FAILED / RUNPOD_TIMEOUT.
"""

from __future__ import annotations

import uuid
from typing import Any, Protocol


class RunPodClient(Protocol):
    """Contract the Phase 3 cloud path will implement."""

    async def dispatch(
        self, *, job_id: uuid.UUID, idempotency_key: str, payload: dict[str, Any]
    ) -> str:
        """Submit the job; returns the RunPod job id (stored on jobs.runpod_job_id)."""
        ...

    async def poll(self, runpod_job_id: str) -> dict[str, Any]:
        """Fetch current status/result for the reconciler path."""
        ...


class NotConfiguredRunPodClient:
    """Placeholder wired in until Phase 3 lands. Never reachable from the
    Phase 2 loop (dispatched is excluded from RUNNABLE_STAGES)."""

    async def dispatch(
        self, *, job_id: uuid.UUID, idempotency_key: str, payload: dict[str, Any]
    ) -> str:
        raise NotImplementedError("RunPod dispatch lands in Phase 3 (runpod_client.py)")

    async def poll(self, runpod_job_id: str) -> dict[str, Any]:
        raise NotImplementedError("RunPod polling reconciler lands in Phase 3")
