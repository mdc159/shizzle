"""FastAPI app factory for the Shizzle control plane (DB-backed since Phase 2)."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from .api.remote import router as remote_router
from .api.routes import router
from .db import create_engine, create_session_factory, init_db
from .db.repository import (
    HeartbeatRepository,
    JobRepository,
    PlaybackTelemetryRepository,
    TrackRepository,
)
from .orchestrator.processing import check_dependencies
from .settings import Settings, get_settings

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        engine = create_engine(settings.database_url)
        session_factory = create_session_factory(engine)
        await init_db(engine)  # creates tables on SQLite; Postgres uses Alembic

        app.state.settings = settings
        app.state.engine = engine
        app.state.job_repo = JobRepository(session_factory)
        app.state.track_repo = TrackRepository(session_factory)
        app.state.playback_telemetry_repo = PlaybackTelemetryRepository(session_factory)
        app.state.heartbeat_repo = HeartbeatRepository(session_factory)

        if settings.shizzle_pipeline == "local":
            missing = check_dependencies()
            if missing:
                logger.warning(
                    "Missing tools: %s — local processing will fail", ", ".join(missing)
                )

        orchestrator_task: asyncio.Task | None = None
        embedded = None
        if settings.shizzle_embedded_orchestrator:
            # Single-container `local` profile: run the orchestrator loop
            # in-process. The `stack` profile runs it as a separate service.
            from .orchestrator import Orchestrator

            embedded = Orchestrator(settings)
            orchestrator_task = asyncio.create_task(embedded.run_forever())
            logger.info("Embedded orchestrator started (%s)", embedded.worker_id)

        logger.info("Data directory: %s", settings.data_dir)
        logger.info("Database: %s", settings.database_url.split("@")[-1])
        yield
        # Shutdown
        if embedded is not None and orchestrator_task is not None:
            embedded.request_stop()
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(orchestrator_task, timeout=10)
            if not orchestrator_task.done():
                orchestrator_task.cancel()
        await engine.dispose()

    app = FastAPI(title="Shizzle Control Plane", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)
    app.include_router(remote_router)
    return app


app = create_app()
