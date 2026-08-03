"""Contract/fault-injection fixtures: REAL Postgres via compose, no mocked DB.

Bring up the database first (from the repo root):

    docker compose -f infra/compose.yml --profile test -p shizzle-test up -d postgres

The suite skips cleanly if Postgres is unreachable. The schema is applied by
running the real Alembic migration (subprocess `alembic upgrade head`) — the
migration itself is under test, not Base.metadata.create_all.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text

from shizzle_server.db import create_engine, create_session_factory
from shizzle_server.db.models import SourceType
from shizzle_server.db.repository import (
    HeartbeatRepository,
    JobRepository,
    TrackRepository,
)

SERVER_DIR = Path(__file__).resolve().parent.parent.parent

# The compose service interpolates ${POSTGRES_USER:-shizzle} etc. — and this
# machine carries global POSTGRES_* env vars from another project, so the
# container may not be shizzle/shizzle. Build the default DSN from the same
# variables compose used; SHIZZLE_TEST_DATABASE_URL overrides everything.
_pg_user = os.environ.get("POSTGRES_USER", "shizzle")
_pg_pass = os.environ.get("POSTGRES_PASSWORD", "shizzle")
_pg_db = os.environ.get("POSTGRES_DB", "shizzle")
PG_URL = os.environ.get(
    "SHIZZLE_TEST_DATABASE_URL",
    f"postgresql+asyncpg://{_pg_user}:{_pg_pass}@127.0.0.1:5433/{_pg_db}",
)

pytestmark = pytest.mark.postgres


def _postgres_reachable() -> bool:
    async def probe() -> bool:
        engine = create_engine(PG_URL)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:
            return False
        finally:
            await engine.dispose()

    return asyncio.run(probe())


@pytest.fixture(scope="session")
def pg_available() -> None:
    if not _postgres_reachable():
        pytest.skip(
            "Postgres not reachable at "
            f"{PG_URL} — run: docker compose -f infra/compose.yml "
            "--profile test -p shizzle-test up -d postgres"
        )


@pytest.fixture(scope="session")
def migrated_database(pg_available) -> str:
    """Reset the schema and apply the real Alembic migration once per session."""

    async def reset() -> None:
        engine = create_engine(PG_URL)
        async with engine.connect() as conn:
            await conn.execution_options(isolation_level="AUTOCOMMIT")
            await conn.execute(text("DROP SCHEMA public CASCADE"))
            await conn.execute(text("CREATE SCHEMA public"))
        await engine.dispose()

    asyncio.run(reset())

    env = {**os.environ, "DATABASE_URL": PG_URL}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=SERVER_DIR,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, f"alembic upgrade failed:\n{result.stderr}"
    return PG_URL


@pytest_asyncio.fixture
async def pg_engine(migrated_database):
    engine = create_engine(PG_URL)
    # Clean slate per test, schema intact.
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "TRUNCATE job_events, jobs, tracks, orchestrator_heartbeats "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def pg_repos(pg_engine):
    sf = create_session_factory(pg_engine)
    return JobRepository(sf), TrackRepository(sf), HeartbeatRepository(sf)


@pytest.fixture
def data_dir(tmp_path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    return d


class OrchestratorProcess:
    """A real `python -m shizzle_server.orchestrator` subprocess.

    kill() is a hard SIGKILL-equivalent (TerminateProcess on Windows) — the
    honest crash simulation the fault-injection tests need.
    """

    def __init__(self, worker_id: str, data_dir: Path, extra_env: dict[str, str]) -> None:
        self.worker_id = worker_id
        env = {
            **os.environ,
            "DATABASE_URL": PG_URL,
            "DATA_DIR": str(data_dir),
            "SHIZZLE_WORKER_ID": worker_id,
            "SHIZZLE_EMBEDDED_ORCHESTRATOR": "false",
            "SHIZZLE_PIPELINE": "test",
            "ORCHESTRATOR_POLL_SECONDS": "0.2",
            "ORCHESTRATOR_LEASE_SECONDS": "4",
            "ORCHESTRATOR_HEARTBEAT_SECONDS": "0.5",
            "ORCHESTRATOR_MAX_ATTEMPTS": "3",
            "ORCHESTRATOR_RETRY_BASE_SECONDS": "1",
            "ORCHESTRATOR_RETRY_CAP_SECONDS": "5",
            **extra_env,
        }
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "shizzle_server.orchestrator"],
            cwd=SERVER_DIR,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

    def kill(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)

    def stop(self) -> None:
        self.kill()  # tests always end hard; durable design must tolerate it

    def assert_running(self) -> None:
        assert self.proc.poll() is None, (
            f"orchestrator {self.worker_id} exited early:\n{self.proc.stdout.read()}"
        )


@pytest.fixture
def spawn_orchestrator(data_dir, migrated_database):
    procs: list[OrchestratorProcess] = []

    def _spawn(worker_id: str | None = None, **extra_env: str) -> OrchestratorProcess:
        wid = worker_id or f"contract-{uuid.uuid4().hex[:6]}"
        p = OrchestratorProcess(wid, data_dir, {k.upper(): v for k, v in extra_env.items()})
        procs.append(p)
        return p

    yield _spawn
    for p in procs:
        p.stop()


async def make_upload_job(job_repo: JobRepository, data_dir: Path):
    job_id = uuid.uuid4()
    job_dir = data_dir / job_id.hex
    job_dir.mkdir(parents=True)
    (job_dir / "source.mp4").write_bytes(b"contract test bytes")
    return await job_repo.create_job(
        job_id=job_id,
        source_type=SourceType.upload,
        source_ref="source.mp4",
        title="Contract Track",
    )


async def wait_for(predicate, timeout: float = 30.0, interval: float = 0.2, desc: str = ""):
    """Poll an async predicate until truthy; return its value."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = await predicate()
        if value:
            return value
        await asyncio.sleep(interval)
    raise AssertionError(f"timeout waiting for {desc or predicate}")
