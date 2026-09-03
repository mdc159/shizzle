"""The durable orchestrator loop.

One iteration: claim a runnable job (SELECT ... FOR UPDATE SKIP LOCKED with a
~120 s lease), then walk it through stage handlers while holding and renewing
the lease. Failures are retried with exponential backoff via next_retry_at up
to a max attempt count, then marked failed with a structured error code.
Killing the process at any point is safe: leases expire, stages are
idempotent, and a peer (or restart) reclaims and resumes the job.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import socket
import time
import uuid

from ..db import create_engine, create_session_factory, init_db
from ..db.models import RUNNABLE_STAGES, Job, JobStage
from ..db.repository import HeartbeatRepository, InvalidTransition, JobRepository
from ..errors import ErrorCode, StageError
from ..settings import Settings, get_settings
from .pipelines import build_pipeline
from .runpod_client import HttpRunPodClient, NotConfiguredRunPodClient
from .stages import HANDLERS, StageContext

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, settings: Settings | None = None, *, worker_id: str | None = None) -> None:
        self.settings = settings or get_settings()
        if self.settings.cloud_pipeline and not (
            self.settings.runpod_api_key and self.settings.runpod_endpoint_id
        ):
            # Fail fast at startup: without RunPod credentials the stack would
            # accept uploads that can never be dispatched or published.
            raise RuntimeError(
                "SHIZZLE_PIPELINE=cloud requires RUNPOD_API_KEY and "
                "RUNPOD_ENDPOINT_ID; refusing to start an orchestrator that "
                "cannot dispatch work"
            )
        self.worker_id = worker_id or os.environ.get(
            "SHIZZLE_WORKER_ID", f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:6]}"
        )
        self.engine = create_engine(self.settings.database_url)
        sf = create_session_factory(self.engine)
        self.jobs = JobRepository(sf)
        self.heartbeats = HeartbeatRepository(sf)
        self.pipeline = build_pipeline(self.settings)
        self.runpod = (
            HttpRunPodClient(
                api_key=self.settings.runpod_api_key,
                endpoint_id=self.settings.runpod_endpoint_id,
                api_base=self.settings.runpod_api_base,
            )
            if (
                self.settings.cloud_pipeline
                and self.settings.runpod_api_key
                and self.settings.runpod_endpoint_id
            )
            else NotConfiguredRunPodClient()
        )
        self._stopping = asyncio.Event()

    # --- lifecycle -----------------------------------------------------------

    def request_stop(self) -> None:
        self._stopping.set()

    async def run_forever(self) -> None:
        """Poll-claim-execute until stopped. Survives transient DB outages."""
        self.settings.data_dir.mkdir(parents=True, exist_ok=True)
        await init_db(self.engine)  # no-op on Postgres (Alembic owns schema)
        logger.info(
            "orchestrator %s started (db=%s, pipeline=%s)",
            self.worker_id,
            self.engine.dialect.name,
            type(self.pipeline).__name__,
        )
        last_beat = 0.0
        while not self._stopping.is_set():
            try:
                now = time.monotonic()
                if now - last_beat >= min(
                    self.settings.orchestrator_heartbeat_seconds,
                    self.settings.orchestrator_poll_seconds * 5,
                ):
                    await self.heartbeats.beat(self.worker_id)
                    last_beat = now

                job = await self.jobs.claim_next(
                    worker_id=self.worker_id,
                    lease_seconds=self.settings.orchestrator_lease_seconds,
                )
                if job is None:
                    await self._sleep(self.settings.orchestrator_poll_seconds)
                    continue
                await self.process_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                # DB down, etc. — durable design: wait and retry, never crash the loop.
                logger.exception("orchestrator loop iteration failed; backing off")
                await self._sleep(max(2.0, self.settings.orchestrator_poll_seconds))
        await self.engine.dispose()
        logger.info("orchestrator %s stopped", self.worker_id)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._stopping.wait(), timeout=seconds)

    # --- job execution -------------------------------------------------------

    async def process_job(self, job: Job) -> None:
        """Walk a claimed job through its stages while renewing the lease."""
        renewer = asyncio.create_task(self._renew_lease_forever(job.id))
        try:
            while job.status in RUNNABLE_STAGES and not self._stopping.is_set():
                handler = HANDLERS[job.status]
                ctx = StageContext(
                    job=job,
                    settings=self.settings,
                    pipeline=self.pipeline,
                    jobs=self.jobs,
                    runpod=self.runpod,
                    worker_id=self.worker_id,
                )
                from_stage = job.status
                started = time.monotonic()
                logger.info("job %s: executing stage %s", job.id, from_stage.value)
                try:
                    next_stage = await handler(ctx)
                except StageError as e:
                    await self._handle_stage_error(job, from_stage, e)
                    return
                except InvalidTransition:
                    # Someone else advanced/completed this job (stale lease,
                    # duplicate completion) — drop it without touching state.
                    logger.info("job %s: state moved under us at %s; yielding", job.id, from_stage)
                    return
                except Exception as e:
                    await self._handle_stage_error(
                        job,
                        from_stage,
                        StageError(ErrorCode.INTERNAL, f"{type(e).__name__}: {e}"),
                    )
                    return

                duration_s = round(time.monotonic() - started, 3)
                if next_stage is None:
                    # Handler parks (e.g. dispatched hand-off): schedule a recheck
                    # without consuming an attempt and yield. park frees the
                    # lease, so the finally-block release_lease no-ops on its
                    # lease_owner guard — no double-fire.
                    await self.jobs.park(
                        job.id,
                        worker_id=self.worker_id,
                        recheck_in_seconds=(
                            ctx.park_seconds or ctx.settings.runpod_poll_seconds
                        ),
                    )
                    return
                if ctx.published:
                    # publish_track already committed publishing -> ready.
                    logger.info("job %s: published, ready (%.2fs)", job.id, duration_s)
                    refreshed = await self.jobs.get_job(job.id)
                    if refreshed is not None:
                        job = refreshed
                    continue
                try:
                    job = await self.jobs.advance(
                        job.id,
                        from_stage=from_stage,
                        to_stage=next_stage,
                        duration_s=duration_s,
                        detail=ctx.detail or None,
                    )
                except InvalidTransition:
                    logger.info("job %s: concurrent advance detected; yielding", job.id)
                    return
        finally:
            renewer.cancel()
            with_suppress = asyncio.gather(renewer, return_exceptions=True)
            await with_suppress
            # If the job is parked (non-terminal, e.g. dispatched hand-off or
            # stop requested mid-walk), free the lease so peers can claim it.
            current = await self.jobs.get_job(job.id)
            if current is not None and current.status in RUNNABLE_STAGES:
                await self.jobs.release_lease(job.id, worker_id=self.worker_id)

    async def _renew_lease_forever(self, job_id: uuid.UUID) -> None:
        interval = max(1.0, self.settings.orchestrator_lease_seconds / 3)
        while True:
            await asyncio.sleep(interval)
            try:
                owned = await self.jobs.renew_lease(
                    job_id,
                    worker_id=self.worker_id,
                    lease_seconds=self.settings.orchestrator_lease_seconds,
                )
                if not owned:
                    logger.warning("job %s: lost lease (expired and reclaimed?)", job_id)
                    return
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("job %s: lease renewal failed", job_id)

    async def _handle_stage_error(self, job: Job, stage: JobStage, err: StageError) -> None:
        attempts_next = job.attempt + 1
        max_attempts = self.settings.orchestrator_max_attempts
        if not err.retryable:
            logger.warning("job %s: non-retryable %s at %s", job.id, err.code, stage.value)
            await self.jobs.fail_job(
                job.id, error_code=err.code, error_detail=err.detail, worker_id=self.worker_id
            )
            return
        if attempts_next >= max_attempts:
            logger.warning(
                "job %s: %s at %s — attempt %d/%d exhausted",
                job.id, err.code, stage.value, attempts_next, max_attempts,
            )
            await self.jobs.fail_job(
                job.id,
                error_code=err.code,
                error_detail=f"{err.detail} (max attempts {max_attempts} exceeded)",
                worker_id=self.worker_id,
            )
            return
        delay = min(
            self.settings.orchestrator_retry_base_seconds * (2**job.attempt),
            self.settings.orchestrator_retry_cap_seconds,
        )
        logger.info(
            "job %s: %s at %s — retry %d/%d in %.1fs",
            job.id, err.code, stage.value, attempts_next, max_attempts, delay,
        )
        await self.jobs.schedule_retry(
            job.id,
            worker_id=self.worker_id,
            error_code=err.code,
            error_detail=err.detail,
            retry_in_seconds=delay,
        )
