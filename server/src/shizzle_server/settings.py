"""Runtime settings for the API and orchestrator (pydantic-settings, env-driven)."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

_DEFAULT_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"


class Settings(BaseSettings):
    """All knobs come from the environment (compose/.env); defaults suit the local profile."""

    model_config = SettingsConfigDict(env_prefix="", extra="ignore")

    # --- storage -------------------------------------------------------------
    # Default: SQLite next to the data dir so the single-container `local`
    # profile works with no Postgres. The `stack` profile overrides with the
    # compose Postgres DSN (postgresql+asyncpg://...).
    database_url: str = f"sqlite+aiosqlite:///{_DEFAULT_DATA_DIR / 'shizzle.db'}"
    data_dir: Path = _DEFAULT_DATA_DIR

    # --- upload guards (carried from k25 lineage) ----------------------------
    max_upload_bytes: int = 2 * 1024 * 1024 * 1024  # 2 GB
    max_duration_seconds: int = 1800  # 30 minutes

    # --- orchestrator --------------------------------------------------------
    # Embedded mode runs the orchestrator loop inside the API process (single
    # container `local` profile). The `stack` profile sets this false and runs
    # `python -m shizzle_server.orchestrator` as its own service.
    shizzle_embedded_orchestrator: bool = True
    orchestrator_poll_seconds: float = 1.0
    orchestrator_lease_seconds: float = 120.0
    orchestrator_heartbeat_seconds: float = 30.0
    orchestrator_max_attempts: int = 3
    orchestrator_retry_base_seconds: float = 5.0
    orchestrator_retry_cap_seconds: float = 300.0
    # Orchestrator is considered dead for /api/health after this long without
    # a heartbeat row update.
    orchestrator_liveness_seconds: float = 90.0

    # --- pipeline selection --------------------------------------------------
    # "local"  — reuse processing.run_pipeline (ffmpeg + Demucs on this host).
    # "test"   — deterministic marker-file pipeline for fault-injection tests
    #            (sleeps, effect counters; see orchestrator/testing.py).
    shizzle_pipeline: str = "local"
    # Fault-injection knobs, read only by the "test" pipeline.
    shizzle_test_stage_sleep: float = 0.0
    shizzle_test_fail_times: int = 0

    processing_profile_version: int = 1


def get_settings() -> Settings:
    """Fresh settings from the environment (cheap; no global cache so tests can override env)."""
    return Settings()
