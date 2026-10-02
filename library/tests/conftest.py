"""Shared fixtures: SQLite-backed unit-test database and repositories.

The contract/fault-injection suite (tests/contract/) uses the real compose
Postgres instead — see tests/contract/conftest.py.
"""

from __future__ import annotations

import uuid

import pytest
import pytest_asyncio

from shizzle_server.db import create_engine, create_session_factory
from shizzle_server.db.models import Base, SourceType
from shizzle_server.db.repository import (
    HeartbeatRepository,
    JobRepository,
    PlaybackTelemetryRepository,
    TrackRepository,
)
from shizzle_server.settings import Settings


@pytest.fixture
def fake_aws_credentials(monkeypatch):
    """Keep this machine's real AWS creds + R2 endpoint override away from moto.

    Shared by every moto-backed S3 fixture (unit, contract, ops importers of
    this conftest keep their own bucket-specific ``s3`` fixtures on top).
    """
    for var, value in {
        "AWS_ACCESS_KEY_ID": "testing" * 5,
        "AWS_SECRET_ACCESS_KEY": "testing" * 6,
        "AWS_SESSION_TOKEN": "testing",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_REGION": "us-east-1",
    }.items():
        monkeypatch.setenv(var, value)
    for var in ("AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_S3", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def settings(tmp_path) -> Settings:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    return Settings(
        database_url=f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        data_dir=data_dir,
        shizzle_embedded_orchestrator=False,
        shizzle_pipeline="test",
        shizzle_allow_test_pipeline=True,  # unit suite opts in (C7 default is off)
        orchestrator_poll_seconds=0.05,
        orchestrator_lease_seconds=5.0,
        orchestrator_heartbeat_seconds=0.5,
        orchestrator_max_attempts=3,
        orchestrator_retry_base_seconds=0.1,
        orchestrator_retry_cap_seconds=1.0,
    )


@pytest_asyncio.fixture
async def engine(settings):
    engine = create_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    return create_session_factory(engine)


@pytest_asyncio.fixture
async def job_repo(session_factory) -> JobRepository:
    return JobRepository(session_factory)


@pytest_asyncio.fixture
async def track_repo(session_factory) -> TrackRepository:
    return TrackRepository(session_factory)


@pytest_asyncio.fixture
async def heartbeat_repo(session_factory) -> HeartbeatRepository:
    return HeartbeatRepository(session_factory)


@pytest_asyncio.fixture
async def playback_telemetry_repo(session_factory) -> PlaybackTelemetryRepository:
    return PlaybackTelemetryRepository(session_factory)


@pytest_asyncio.fixture
async def upload_job(job_repo, settings):
    """A pending upload-sourced job whose source file exists on disk."""
    job_id = uuid.uuid4()
    job_dir = settings.data_dir / job_id.hex
    job_dir.mkdir(parents=True)
    (job_dir / "source.mp4").write_bytes(b"fake video bytes")
    return await job_repo.create_job(
        job_id=job_id,
        source_type=SourceType.upload,
        source_ref="source.mp4",
        title="Test Track",
    )
