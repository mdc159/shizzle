"""
S3 Multipart Upload module for Karaoke Agent.

Robust multipart uploader designed for S3-compatible APIs backed by POSIX filesystems
(like Runpod Network Volumes) that have slower checksum/merge operations.

Key features:
- HTTP 524 timeout retry with exponential backoff
- Complete multipart upload with timeout doubling
- HeadObject verification after upload
- 507 Insufficient Storage detection and abort
- Upload resumability via UploadId preservation
- ThreadPoolExecutor(4) for concurrent part uploads
- Progress callback support

Refactored from examples/upload_large_file.py for use as a library module.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Any

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError, ConnectTimeoutError, ReadTimeoutError

if TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client

logger = logging.getLogger(__name__)


# Type alias for progress callback
ProgressCallback = Callable[[int, int, float], None]  # (completed_parts, total_parts, eta_seconds)


@dataclass
class S3UploadConfig:
    """Configuration for S3 multipart uploads."""

    endpoint: str
    bucket: str
    access_key: str
    secret_key: str
    region: str = "us-east-1"
    part_size_mb: int = 50
    max_retries: int = 5
    max_concurrency: int = 4

    @property
    def part_size_bytes(self) -> int:
        """Return part size in bytes."""
        return self.part_size_mb * 1024 * 1024


@dataclass
class UploadResult:
    """Result of a multipart upload operation."""

    success: bool
    bucket: str
    key: str
    file_size: int
    upload_id: str | None = None
    elapsed_seconds: float = 0.0
    speed_mb_per_sec: float = 0.0
    error: str | None = None

    @property
    def s3_uri(self) -> str:
        """Return S3 URI for the uploaded object."""
        return f"s3://{self.bucket}/{self.key}"


class S3MultipartUploader:
    """
    Robust multipart uploader for S3-compatible storage.

    Handles slow POSIX-backed S3 endpoints with:
    - 524 timeout retries
    - Completion timeout doubling
    - HeadObject verification
    - 507 storage error detection
    - Concurrent part uploads
    """

    def __init__(
        self,
        config: S3UploadConfig,
        progress_callback: ProgressCallback | None = None,
    ) -> None:
        """
        Initialize the multipart uploader.

        Args:
            config: S3 upload configuration
            progress_callback: Optional callback for upload progress
                              Called with (completed_parts, total_parts, eta_seconds)
        """
        self.config = config
        self.progress_callback = progress_callback

        self._progress_lock = Lock()
        self._parts_completed = 0

        # Initialize boto3 session and client
        self._session = boto3.session.Session(
            aws_access_key_id=config.access_key,
            aws_secret_access_key=config.secret_key,
            region_name=config.region,
        )
        self._botocore_config = Config(
            region_name=config.region,
            retries={"max_attempts": config.max_retries, "mode": "standard"},
        )
        self._s3: S3Client = self._session.client(
            "s3",
            config=self._botocore_config,
            endpoint_url=config.endpoint,
        )

        # Current upload state
        self._upload_id: str | None = None

    @property
    def upload_id(self) -> str | None:
        """Return current upload ID for potential resumption."""
        return self._upload_id

    # -------------------------------------------------------------------------
    # Error Detection Helpers
    # -------------------------------------------------------------------------
    @staticmethod
    def _is_524_error(exc: Exception) -> bool:
        """Return True if exception is HTTP 524 timeout response."""
        if isinstance(exc, ClientError):
            meta = exc.response.get("ResponseMetadata", {})
            return meta.get("HTTPStatusCode") == 524
        return False

    @staticmethod
    def _is_507_error(exc: Exception) -> bool:
        """Return True if exception is HTTP 507 Insufficient Storage."""
        if isinstance(exc, ClientError):
            meta = exc.response.get("ResponseMetadata", {})
            return meta.get("HTTPStatusCode") == 507
        return False

    @staticmethod
    def _is_no_such_upload_error(exc: Exception) -> bool:
        """Return True if exception indicates missing multipart upload."""
        if isinstance(exc, ClientError):
            err = exc.response.get("Error", {})
            return err.get("Code") == "NoSuchUpload"
        return False

    @staticmethod
    def _human_speed(num_bytes: int, seconds: float) -> float:
        """Return upload speed in MB/s."""
        if seconds <= 0:
            return float("inf")
        return (num_bytes / (1024 * 1024)) / seconds

    # -------------------------------------------------------------------------
    # Retry Logic
    # -------------------------------------------------------------------------
    def _call_with_524_retry(self, description: str, func: Callable[[], Any]) -> Any:
        """
        Call function with retries on HTTP 524 or timeout errors.

        Args:
            description: Description for logging
            func: Function to call

        Returns:
            Result of function call

        Raises:
            Last exception if all retries exhausted
        """
        for attempt in range(1, self.config.max_retries + 1):
            try:
                return func()
            except ClientError as exc:
                if self._is_524_error(exc):
                    logger.warning(f"{description}: received 524 response (attempt {attempt})")
                    if attempt == self.config.max_retries:
                        logger.error(f"{description}: exceeded max_retries for 524")
                        raise
                    backoff = 2**attempt
                    logger.info(f"{description}: retrying in {backoff}s...")
                    time.sleep(backoff)
                    continue
                raise
            except (ReadTimeoutError, ConnectTimeoutError) as exc:
                logger.warning(f"{description}: request timed out (attempt {attempt}): {exc}")
                if attempt == self.config.max_retries:
                    logger.error(f"{description}: exceeded max_retries for timeout")
                    raise
                backoff = 2**attempt
                logger.info(f"{description}: retrying in {backoff}s...")
                time.sleep(backoff)

        # Should not reach here, but satisfy type checker
        raise RuntimeError(f"{description}: retry loop exited unexpectedly")

    def _complete_with_timeout_retry(
        self,
        parts_sorted: list[dict[str, Any]],
        initial_timeout: int,
        expected_size: int,
    ) -> None:
        """
        Complete multipart upload with timeout doubling on failures.

        Args:
            parts_sorted: List of part dictionaries sorted by PartNumber
            initial_timeout: Initial timeout in seconds
            expected_size: Expected file size for verification

        Raises:
            RuntimeError: If completion fails after all retries
        """
        if self._upload_id is None:
            raise RuntimeError("upload_id not set")

        timeout = initial_timeout
        cfg = self._botocore_config
        last_exc: Exception | None = None

        for attempt in range(1, self.config.max_retries + 1):
            cfg = cfg.merge(Config(read_timeout=timeout, connect_timeout=timeout))
            client = self._session.client("s3", config=cfg, endpoint_url=self.config.endpoint)

            try:
                client.complete_multipart_upload(
                    Bucket=self.config.bucket,
                    Key=self._current_key,
                    UploadId=self._upload_id,
                    MultipartUpload={"Parts": parts_sorted},
                )
                self._s3 = client
                self._botocore_config = cfg
                return
            except (ReadTimeoutError, ConnectTimeoutError) as exc:
                last_exc = exc
                no_such_upload = False
                logger.warning(f"complete_multipart_upload timed out after {timeout}s: {exc}")
            except (ClientError, BotoCoreError) as exc:
                last_exc = exc
                no_such_upload = self._is_no_such_upload_error(exc)
                logger.warning(f"complete_multipart_upload failed (attempt {attempt}): {exc}")

            # Check if upload completed despite timeout
            if no_such_upload:
                logger.info("Upload session missing; checking object state immediately")
            else:
                logger.info(f"Waiting {timeout}s before checking object state")
                time.sleep(timeout)

            try:
                # Capture client in closure to avoid late binding issue
                current_client = client
                head = self._call_with_524_retry(
                    "head_object",
                    lambda c=current_client: c.head_object(
                        Bucket=self.config.bucket, Key=self._current_key
                    ),
                )
                uploaded_size = head.get("ContentLength")
                if uploaded_size == expected_size:
                    logger.info("HeadObject confirms multipart upload merge completed")
                    self._s3 = client
                    self._botocore_config = cfg
                    return
                logger.info("HeadObject size mismatch; will retry complete_multipart_upload")
            except Exception as head_exc:
                logger.info(f"head_object failed after error: {head_exc}")

            if attempt == self.config.max_retries:
                raise last_exc or RuntimeError("Exceeded max_retries without completing upload")

            timeout *= 2
            logger.info(f"Increasing timeout to {timeout}s and retrying")

    # -------------------------------------------------------------------------
    # Part Upload
    # -------------------------------------------------------------------------
    def _upload_part(
        self,
        file_path: Path,
        part_number: int,
        offset: int,
        bytes_to_read: int,
        total_parts: int,
        start_time: float,
    ) -> dict[str, Any]:
        """
        Upload a single part with exponential-backoff retries.

        Args:
            file_path: Path to local file
            part_number: Part number (1-indexed)
            offset: Byte offset in file
            bytes_to_read: Number of bytes to read
            total_parts: Total number of parts
            start_time: Upload start time for ETA calculation

        Returns:
            Dict with PartNumber and ETag

        Raises:
            RuntimeError: If upload fails after all retries or 507 error
        """
        if self._upload_id is None:
            raise RuntimeError("upload_id not set")

        for attempt in range(1, self.config.max_retries + 1):
            try:
                logger.info(
                    f"Part {part_number}: reading bytes {offset}-{offset + bytes_to_read} "
                    f"(attempt {attempt})"
                )
                with open(file_path, "rb") as f:
                    f.seek(offset)
                    data = f.read(bytes_to_read)

                resp = self._s3.upload_part(
                    Bucket=self.config.bucket,
                    Key=self._current_key,
                    PartNumber=part_number,
                    UploadId=self._upload_id,
                    Body=data,
                )
                etag = resp["ETag"]

                # Update progress
                with self._progress_lock:
                    self._parts_completed += 1
                    progress_pct = 100.0 * self._parts_completed / total_parts

                elapsed = time.time() - start_time
                progress_fraction = part_number / total_parts
                if progress_fraction > 0:
                    remaining = max(0, elapsed * (1 / progress_fraction - 1))
                else:
                    remaining = 0

                eta_str = time.strftime("%Hh %Mm %Ss", time.gmtime(remaining))
                logger.info(
                    f"Part {part_number}: uploaded, progress: {progress_pct:.1f}%, "
                    f"est time remaining: {eta_str}"
                )

                # Call progress callback if provided
                if self.progress_callback:
                    self.progress_callback(self._parts_completed, total_parts, remaining)

                return {"PartNumber": part_number, "ETag": etag}

            except (BotoCoreError, ClientError) as exc:
                if self._is_507_error(exc):
                    logger.error(f"Part {part_number}: received 507 Insufficient Storage; aborting")
                    raise RuntimeError("Server reported insufficient storage") from exc

                if self._is_524_error(exc):
                    logger.warning(f"Part {part_number}: received 524 response (attempt {attempt})")
                else:
                    logger.warning(f"Part {part_number}: attempt {attempt} failed: {exc}")

                if attempt == self.config.max_retries:
                    logger.error(
                        f"Part {part_number}: exceeded max_retries ({self.config.max_retries})"
                    )
                    raise

                backoff = 2**attempt
                logger.info(f"Part {part_number}: retrying in {backoff}s...")
                time.sleep(backoff)

        # Should not reach here
        raise RuntimeError(f"Part {part_number}: retry loop exited unexpectedly")

    # -------------------------------------------------------------------------
    # Main Upload Method
    # -------------------------------------------------------------------------
    def upload(
        self,
        file_path: str | Path,
        key: str,
        content_type: str | None = None,
    ) -> UploadResult:
        """
        Upload a file using multipart upload.

        Args:
            file_path: Path to local file
            key: S3 object key
            content_type: Optional content type (auto-detected if not provided)

        Returns:
            UploadResult with success status and metadata

        Note:
            On failure, upload_id is preserved for potential resumption.
            Check result.upload_id to resume a failed upload.
        """
        file_path = Path(file_path)
        self._current_key = key
        self._parts_completed = 0

        if not file_path.exists():
            return UploadResult(
                success=False,
                bucket=self.config.bucket,
                key=key,
                file_size=0,
                error=f"File not found: {file_path}",
            )

        file_size = file_path.stat().st_size
        total_parts = math.ceil(file_size / self.config.part_size_bytes)

        logger.info(
            f"Uploading to region: {self.config.region}; bucket: {self.config.bucket}; key: {key}"
        )
        logger.info(
            f"File size: {file_size} bytes; will upload in {total_parts} parts "
            f"of up to {self.config.part_size_bytes} bytes each"
        )

        start_time = time.time()

        # Calculate completion timeout based on file size
        file_gb = file_size / float(1024**3)
        completion_timeout = max(60, int(math.ceil(file_gb) * 5))

        try:
            # Create multipart upload
            create_args: dict[str, Any] = {"Bucket": self.config.bucket, "Key": key}
            if content_type:
                create_args["ContentType"] = content_type

            resp = self._call_with_524_retry(
                "create_multipart_upload",
                lambda: self._s3.create_multipart_upload(**create_args),
            )
            self._upload_id = resp["UploadId"]
            logger.info(f"Initiated multipart upload: UploadId={self._upload_id}")

            # Upload parts concurrently
            parts: list[dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=self.config.max_concurrency) as executor:
                futures = {}
                for part_num in range(1, total_parts + 1):
                    offset = (part_num - 1) * self.config.part_size_bytes
                    chunk_size = min(self.config.part_size_bytes, file_size - offset)
                    futures[
                        executor.submit(
                            self._upload_part,
                            file_path=file_path,
                            part_number=part_num,
                            offset=offset,
                            bytes_to_read=chunk_size,
                            total_parts=total_parts,
                            start_time=start_time,
                        )
                    ] = part_num

                for fut in as_completed(futures):
                    part = fut.result()
                    parts.append(part)

            # Verify all parts uploaded
            def fetch_parts() -> list[dict[str, Any]]:
                paginator = self._s3.get_paginator("list_parts")
                found: list[dict[str, Any]] = []
                for page in paginator.paginate(
                    Bucket=self.config.bucket,
                    Key=key,
                    UploadId=self._upload_id,
                ):
                    found.extend(page.get("Parts", []))
                return found

            seen = self._call_with_524_retry("list_parts", fetch_parts)
            logger.info(f"Verified {len(seen)} of {total_parts} parts uploaded")

            if len(seen) != total_parts:
                raise RuntimeError(f"Expected {total_parts} parts but saw {len(seen)}")

            # Complete multipart upload
            parts_sorted = sorted(parts, key=lambda x: x["PartNumber"])
            logger.info("Sending complete_multipart_upload request")
            self._complete_with_timeout_retry(
                parts_sorted=parts_sorted,
                initial_timeout=completion_timeout,
                expected_size=file_size,
            )

            # Final verification
            head = self._call_with_524_retry(
                "head_object",
                lambda: self._s3.head_object(Bucket=self.config.bucket, Key=key),
            )
            uploaded_size = head.get("ContentLength")
            if uploaded_size != file_size:
                raise RuntimeError(
                    f"Size mismatch: remote object is {uploaded_size} bytes, "
                    f"but local file is {file_size} bytes"
                )
            logger.info(f"Verified upload: remote size {uploaded_size} matches local {file_size}")

            elapsed = time.time() - start_time
            speed = self._human_speed(file_size, elapsed)
            duration = time.strftime("%Hh %Mm %Ss", time.gmtime(elapsed))
            logger.info(f"Upload Speed {speed:.2f} MB/s, Duration {duration}")

            # Clear upload ID on success
            self._upload_id = None

            return UploadResult(
                success=True,
                bucket=self.config.bucket,
                key=key,
                file_size=file_size,
                elapsed_seconds=elapsed,
                speed_mb_per_sec=speed,
            )

        except Exception as exc:
            logger.error(f"Upload failed: {exc}")
            if self._upload_id:
                logger.info(f"UploadId {self._upload_id} preserved for potential resumption")

            elapsed = time.time() - start_time
            return UploadResult(
                success=False,
                bucket=self.config.bucket,
                key=key,
                file_size=file_size,
                upload_id=self._upload_id,
                elapsed_seconds=elapsed,
                error=str(exc),
            )

    def abort_upload(self, key: str, upload_id: str | None = None) -> bool:
        """
        Abort a multipart upload.

        Args:
            key: S3 object key
            upload_id: Upload ID to abort (defaults to current upload_id)

        Returns:
            True if abort was successful
        """
        uid = upload_id or self._upload_id
        if not uid:
            logger.warning("No upload_id to abort")
            return False

        try:
            self._s3.abort_multipart_upload(
                Bucket=self.config.bucket,
                Key=key,
                UploadId=uid,
            )
            logger.info(f"Aborted multipart upload: {uid}")
            if uid == self._upload_id:
                self._upload_id = None
            return True
        except Exception as exc:
            logger.error(f"Failed to abort upload {uid}: {exc}")
            return False


def create_uploader_from_config(
    config: Any,
    progress_callback: ProgressCallback | None = None,
) -> S3MultipartUploader:
    """
    Create an S3MultipartUploader from an S3Config-like settings object.

    The original karaoke_agent.config.S3Config was not salvaged; any object
    exposing endpoint/bucket/access_key/secret_key/region/part_size_mb/
    max_retries/max_concurrency attributes works.

    Args:
        config: S3Config-like settings object
        progress_callback: Optional progress callback

    Returns:
        Configured S3MultipartUploader instance
    """

    upload_config = S3UploadConfig(
        endpoint=config.endpoint,
        bucket=config.bucket,
        access_key=config.access_key,
        secret_key=config.secret_key,
        region=config.region,
        part_size_mb=config.part_size_mb,
        max_retries=config.max_retries,
        max_concurrency=config.max_concurrency,
    )
    return S3MultipartUploader(upload_config, progress_callback)
