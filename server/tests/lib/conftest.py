"""
Pytest fixtures for shizzle_server.lib tests.

Salvaged from k25/archive/agent/tests/conftest.py and trimmed to the fixtures
the carried tests (test_s3_multipart, test_circuit_breaker) actually use.
Fixtures depending on non-salvaged modules (karaoke_agent.config,
karaoke_agent.supabase_client, moto-backed S3 fixtures) were dropped.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Generator
from pathlib import Path

import pytest

from shizzle_server.lib.circuit_breaker import CircuitBreaker
from shizzle_server.lib.s3_multipart import S3UploadConfig

# =============================================================================
# Environment Setup
# =============================================================================


@pytest.fixture(autouse=True)
def clean_env() -> Generator[None, None, None]:
    """Clean environment variables before each test."""
    original_env = os.environ.copy()

    test_env = {
        "AWS_S3_ENDPOINT": "https://s3.test.com",
        "AWS_S3_BUCKET": "test-bucket",
        "AWS_ACCESS_KEY_ID": "test-access-key",
        "AWS_SECRET_ACCESS_KEY": "test-secret-key",
        "AWS_REGION": "us-east-1",
    }

    for key, value in test_env.items():
        os.environ[key] = value

    yield

    os.environ.clear()
    os.environ.update(original_env)


# =============================================================================
# S3 Fixtures
# =============================================================================


@pytest.fixture
def s3_upload_config() -> S3UploadConfig:
    """Create S3 upload configuration for tests."""
    return S3UploadConfig(
        endpoint="http://localhost:5000",  # Will be mocked
        bucket="test-bucket",
        access_key="test-key",
        secret_key="test-secret",
        region="us-east-1",
        part_size_mb=5,
        max_retries=2,
        max_concurrency=2,
    )


# =============================================================================
# File Fixtures
# =============================================================================


@pytest.fixture
def temp_dir() -> Generator[Path, None, None]:
    """Create temporary directory for tests."""
    with tempfile.TemporaryDirectory() as tmpdir:
        yield Path(tmpdir)


@pytest.fixture
def sample_video_file(temp_dir: Path) -> Path:
    """Create sample video file for upload tests."""
    video_path = temp_dir / "test_video.mp4"
    # Create 6MB file (larger than minimum part size)
    with open(video_path, "wb") as f:
        f.write(b"0" * (6 * 1024 * 1024))
    return video_path


@pytest.fixture
def small_file(temp_dir: Path) -> Path:
    """Create small file for simple upload tests."""
    file_path = temp_dir / "small_file.txt"
    file_path.write_text("Hello, World!")
    return file_path


# =============================================================================
# Circuit Breaker Fixtures
# =============================================================================


@pytest.fixture
def circuit_breaker() -> CircuitBreaker[object]:
    """Create circuit breaker for testing."""
    return CircuitBreaker(
        failure_threshold=3,
        timeout_seconds=1,  # Fast timeout for tests
        name="test",
    )


# =============================================================================
# Markers
# =============================================================================


def pytest_configure(config: pytest.Config) -> None:
    """Configure custom markers."""
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (may require external services)",
    )
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (longer running)",
    )
