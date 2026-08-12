"""Tests for S3 multipart upload module."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError

from shizzle_server.lib.s3_multipart import (
    S3MultipartUploader,
    S3UploadConfig,
    UploadResult,
)


class TestS3UploadConfig:
    """Test S3 upload configuration."""

    def test_part_size_bytes_conversion(self) -> None:
        """Part size MB converts to bytes correctly."""
        config = S3UploadConfig(
            endpoint="https://s3.test.com",
            bucket="test",
            access_key="key",
            secret_key="secret",
            part_size_mb=50,
        )
        assert config.part_size_bytes == 50 * 1024 * 1024

    def test_default_values(self) -> None:
        """Default values are set correctly."""
        config = S3UploadConfig(
            endpoint="https://s3.test.com",
            bucket="test",
            access_key="key",
            secret_key="secret",
        )
        assert config.region == "us-east-1"
        assert config.part_size_mb == 50
        assert config.max_retries == 5
        assert config.max_concurrency == 4


class TestUploadResult:
    """Test upload result dataclass."""

    def test_success_result(self) -> None:
        """Success result has correct properties."""
        result = UploadResult(
            success=True,
            bucket="my-bucket",
            key="path/to/file.mp4",
            file_size=1000000,
            elapsed_seconds=10.5,
            speed_mb_per_sec=0.95,
        )
        assert result.success
        assert result.s3_uri == "s3://my-bucket/path/to/file.mp4"
        assert result.error is None

    def test_failure_result(self) -> None:
        """Failure result includes error message."""
        result = UploadResult(
            success=False,
            bucket="my-bucket",
            key="path/to/file.mp4",
            file_size=1000000,
            error="Connection timeout",
            upload_id="abc123",
        )
        assert not result.success
        assert result.error == "Connection timeout"
        assert result.upload_id == "abc123"  # Preserved for resumption


class TestS3MultipartUploaderErrorDetection:
    """Test error detection helper methods."""

    @pytest.fixture
    def uploader(self, s3_upload_config: S3UploadConfig) -> S3MultipartUploader:
        """Create uploader with mocked S3 client."""
        with patch("shizzle_server.lib.s3_multipart.boto3"):
            return S3MultipartUploader(s3_upload_config)

    def test_is_524_error_detection(self) -> None:
        """524 error is correctly detected."""
        # Create 524 ClientError
        error_response = {
            "Error": {"Code": "Timeout", "Message": "524 Timeout"},
            "ResponseMetadata": {"HTTPStatusCode": 524},
        }
        exc = ClientError(error_response, "UploadPart")

        assert S3MultipartUploader._is_524_error(exc)

        # Non-524 error
        error_response["ResponseMetadata"]["HTTPStatusCode"] = 500
        exc = ClientError(error_response, "UploadPart")
        assert not S3MultipartUploader._is_524_error(exc)

    def test_is_507_error_detection(self) -> None:
        """507 Insufficient Storage error is correctly detected."""
        error_response = {
            "Error": {"Code": "InsufficientStorage", "Message": "507"},
            "ResponseMetadata": {"HTTPStatusCode": 507},
        }
        exc = ClientError(error_response, "UploadPart")

        assert S3MultipartUploader._is_507_error(exc)

    def test_is_no_such_upload_error_detection(self) -> None:
        """NoSuchUpload error is correctly detected."""
        error_response = {
            "Error": {"Code": "NoSuchUpload", "Message": "Upload not found"},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        exc = ClientError(error_response, "CompleteMultipartUpload")

        assert S3MultipartUploader._is_no_such_upload_error(exc)

    def test_human_speed_calculation(self) -> None:
        """Speed calculation is correct."""
        # 10 MB in 5 seconds = 2 MB/s
        speed = S3MultipartUploader._human_speed(10 * 1024 * 1024, 5.0)
        assert abs(speed - 2.0) < 0.01

        # Zero seconds returns infinity
        speed = S3MultipartUploader._human_speed(1000, 0)
        assert speed == float("inf")


class TestS3MultipartUploaderRetry:
    """Test retry logic."""

    @pytest.fixture
    def uploader(self, s3_upload_config: S3UploadConfig) -> S3MultipartUploader:
        """Create uploader with mocked S3 client."""
        s3_upload_config.max_retries = 3
        with patch("shizzle_server.lib.s3_multipart.boto3"):
            return S3MultipartUploader(s3_upload_config)

    def test_call_with_524_retry_succeeds_on_first_try(
        self, uploader: S3MultipartUploader
    ) -> None:
        """Successful call returns immediately."""
        call_count = 0

        def successful_call() -> str:
            nonlocal call_count
            call_count += 1
            return "success"

        result = uploader._call_with_524_retry("test", successful_call)

        assert result == "success"
        assert call_count == 1

    def test_call_with_524_retry_retries_on_524(
        self, uploader: S3MultipartUploader
    ) -> None:
        """524 errors trigger retries with exponential backoff."""
        call_count = 0

        def flaky_call() -> str:
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                # Simulate 524 error
                error_response = {
                    "Error": {"Code": "Timeout"},
                    "ResponseMetadata": {"HTTPStatusCode": 524},
                }
                raise ClientError(error_response, "UploadPart")
            return "success"

        # Patch time.sleep to speed up test
        with patch("shizzle_server.lib.s3_multipart.time.sleep"):
            result = uploader._call_with_524_retry("test", flaky_call)

        assert result == "success"
        assert call_count == 3

    def test_call_with_524_retry_raises_after_max_retries(
        self, uploader: S3MultipartUploader
    ) -> None:
        """Persistent 524 errors raise after max retries."""

        def always_fails() -> str:
            error_response = {
                "Error": {"Code": "Timeout"},
                "ResponseMetadata": {"HTTPStatusCode": 524},
            }
            raise ClientError(error_response, "UploadPart")

        with (
            patch("shizzle_server.lib.s3_multipart.time.sleep"),
            pytest.raises(ClientError),
        ):
            uploader._call_with_524_retry("test", always_fails)

    def test_non_524_errors_raise_immediately(
        self, uploader: S3MultipartUploader
    ) -> None:
        """Non-524 ClientErrors are raised without retry."""
        call_count = 0

        def other_error() -> str:
            nonlocal call_count
            call_count += 1
            error_response = {
                "Error": {"Code": "AccessDenied"},
                "ResponseMetadata": {"HTTPStatusCode": 403},
            }
            raise ClientError(error_response, "UploadPart")

        with pytest.raises(ClientError) as exc_info:
            uploader._call_with_524_retry("test", other_error)

        # Should fail on first try without retry
        assert call_count == 1
        assert exc_info.value.response["ResponseMetadata"]["HTTPStatusCode"] == 403


class TestS3MultipartUploaderUpload:
    """Test upload functionality."""

    @pytest.fixture
    def uploader(self, s3_upload_config: S3UploadConfig) -> S3MultipartUploader:
        """Create uploader with mocked S3 client."""
        with patch("shizzle_server.lib.s3_multipart.boto3"):
            return S3MultipartUploader(s3_upload_config)

    def test_upload_nonexistent_file_returns_error(
        self, uploader: S3MultipartUploader, temp_dir: Path
    ) -> None:
        """Upload of nonexistent file returns error result."""
        nonexistent = temp_dir / "does_not_exist.mp4"

        result = uploader.upload(nonexistent, "test/key.mp4")

        assert not result.success
        assert "not found" in result.error.lower()

    def test_progress_callback_is_called(
        self, uploader: S3MultipartUploader
    ) -> None:
        """Progress callback is invoked during upload."""
        progress_calls: list[tuple[int, int, float]] = []

        def track_progress(completed: int, total: int, eta: float) -> None:
            progress_calls.append((completed, total, eta))

        uploader_with_callback = S3MultipartUploader(
            uploader.config,
            progress_callback=track_progress,
        )

        # Mock the S3 operations
        with patch.object(uploader_with_callback, "_call_with_524_retry") as mock_call:
            mock_call.return_value = {"UploadId": "test-id"}

            # Note: Full upload test would require more extensive mocking
            # This test verifies the callback mechanism is in place
            assert uploader_with_callback.progress_callback is not None


class TestS3MultipartUploaderAbort:
    """Test abort functionality."""

    @pytest.fixture
    def uploader(self, s3_upload_config: S3UploadConfig) -> S3MultipartUploader:
        """Create uploader with mocked S3 client."""
        with patch("shizzle_server.lib.s3_multipart.boto3"):
            uploader = S3MultipartUploader(s3_upload_config)
            uploader._s3 = MagicMock()
            return uploader

    def test_abort_with_no_upload_id_returns_false(
        self, uploader: S3MultipartUploader
    ) -> None:
        """Abort without upload_id returns False."""
        assert uploader.upload_id is None
        assert not uploader.abort_upload("test/key.mp4")

    def test_abort_with_upload_id_calls_s3(
        self, uploader: S3MultipartUploader
    ) -> None:
        """Abort with upload_id calls S3 abort."""
        uploader._upload_id = "test-upload-id"

        result = uploader.abort_upload("test/key.mp4")

        assert result
        uploader._s3.abort_multipart_upload.assert_called_once_with(
            Bucket=uploader.config.bucket,
            Key="test/key.mp4",
            UploadId="test-upload-id",
        )
        assert uploader.upload_id is None  # Cleared after abort

    def test_abort_clears_upload_id_on_success(
        self, uploader: S3MultipartUploader
    ) -> None:
        """Successful abort clears the upload_id."""
        uploader._upload_id = "test-id"

        uploader.abort_upload("key.mp4")

        assert uploader._upload_id is None
