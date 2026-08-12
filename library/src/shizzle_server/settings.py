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

    # RunPod serverless settings, used only when shizzle_pipeline="cloud".
    runpod_api_key: str = ""
    runpod_endpoint_id: str = ""
    runpod_api_base: str = "https://api.runpod.ai/v2"
    runpod_poll_seconds: float = 10.0
    runpod_queue_timeout_seconds: float = 900.0
    runpod_worker_stall_seconds: float = 300.0

    processing_profile_version: int = 1

    # --- auth (design spec §7) -----------------------------------------------
    # One shared passcode friends enter once per device. When empty (the local
    # and test default) the gate is OFF — every route is open, which keeps the
    # single-container profile and the unit suite working with no secrets. Set
    # a real value on the VPS to turn the gate on. Rotating the passcode (its
    # value is bound into every token's signature) revokes all issued tokens.
    shizzle_passcode: str = ""
    auth_version: int = 1
    token_signing_secret: str = "dev-insecure-change-me"
    auth_token_ttl_seconds: int = 7 * 24 * 3600

    # --- AWS / S3 media ------------------------------------------------------
    # The imported legacy library lives in `karaoke-pimpshizzle` under
    # `tracks/{id}/{generation}/…` (see ops/import_legacy_library.py). The
    # media-manifest route reads manifest.json from here.
    aws_region: str = "us-east-1"
    # Machine gotcha: a global AWS_ENDPOINT_URL points this PC at Cloudflare R2.
    # Empty means "use real AWS"; the S3 client factory pops the env override.
    aws_endpoint_url: str = ""
    s3_media_bucket: str = "karaoke-pimpshizzle"

    # --- CloudFront signed-cookie media delivery (design spec §3/§7) ---------
    # Media is served through CloudFront (OAC over the private bucket + a
    # trusted key group requiring signed cookies). Caddy reverse-proxies the
    # same-origin path `media_cookie_path` (/cdn) to the distribution so the
    # signed cookies — set on shizzle.systems — ride along same-origin.
    cloudfront_domain: str = ""  # e.g. d2488k8kjndpsy.cloudfront.net
    cloudfront_key_pair_id: str = ""
    cloudfront_private_key_path: str = ""
    # Same-origin path under shizzle.systems that Caddy maps to the CDN. The
    # signed cookies are scoped here; the rewritten manifest points media at it.
    media_cookie_path: str = "/cdn"
    # Signed-cookie validity. Refreshed by /api/media/session.
    media_ttl_seconds: int = 24 * 3600

    @property
    def cloud_pipeline(self) -> bool:
        return self.shizzle_pipeline == "cloud"

    @property
    def auth_enabled(self) -> bool:
        return bool(self.shizzle_passcode)

    @property
    def cloudfront_enabled(self) -> bool:
        return bool(
            self.cloudfront_domain
            and self.cloudfront_key_pair_id
            and self.cloudfront_private_key_path
        )

    @property
    def cloudfront_resource_base(self) -> str:
        """The URL prefix CloudFront sees (behind the Caddy /cdn proxy)."""
        return f"https://{self.cloudfront_domain}"


def get_settings() -> Settings:
    """Fresh settings from the environment (cheap; no global cache so tests can override env)."""
    return Settings()
