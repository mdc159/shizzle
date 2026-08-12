"""
S3 operations for the Shizzle GPU worker.

Handles download/upload with verification. Every uploaded object's sha256 is
computed locally and recorded in the upload record; the publisher verifies
staged objects server-side against these checksums before promotion.
"""

import contextlib
import hashlib
import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

import boto3

Heartbeat = Callable[[str], None]


def _transfer_callback(
    heartbeat: Heartbeat | None, label: str, total_bytes: int
) -> Callable[[int], None] | None:
    if heartbeat is None or total_bytes <= 0:
        return None
    transferred = 0
    last_bucket = -1

    def callback(bytes_amount: int) -> None:
        nonlocal transferred, last_bucket
        with contextlib.suppress(Exception):
            transferred += bytes_amount
            bucket = min(20, int(20 * transferred / total_bytes))
            if bucket != last_bucket:
                heartbeat(f"{label}: {bucket * 5}%")
                last_bucket = bucket

    return callback


def sha256_file(path: Path) -> str:
    """Compute the sha256 hex digest of a file (streamed)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _env(name: str) -> str:
    """Get required environment variable."""
    v = os.getenv(name)
    if not v:
        raise RuntimeError(f"Missing env var: {name}")
    return v


def create_s3_client() -> Any:
    """Create and return boto3 S3 client."""
    return boto3.client(
        "s3",
        aws_access_key_id=_env("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=_env("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_REGION", "us-east-1"),
    )


def verify_download(local_path: Path) -> None:
    """
    Verify S3 download completed successfully.

    Args:
        local_path: Path to downloaded file

    Raises:
        RuntimeError: If file doesn't exist or is too small
    """
    if not local_path.exists():
        raise RuntimeError(f"S3 download failed: file does not exist at {local_path}")

    file_size_mb = local_path.stat().st_size / (1024 ** 2)
    if file_size_mb < 0.1:
        raise RuntimeError(f"S3 download failed: file too small ({file_size_mb:.2f} MB)")

    print(f"Verified S3 download: {local_path.name} ({file_size_mb:.1f} MB)", flush=True)


def verify_upload(s3: Any, bucket: str, s3_key: str, local_path: Path) -> None:
    """
    Verify S3 upload completed successfully by comparing file sizes.

    Args:
        s3: boto3 S3 client
        bucket: S3 bucket name
        s3_key: S3 object key
        local_path: Path to local file that was uploaded

    Raises:
        RuntimeError: If upload verification fails
    """
    local_size = local_path.stat().st_size

    try:
        response = s3.head_object(Bucket=bucket, Key=s3_key)
        remote_size = response["ContentLength"]

        if local_size != remote_size:
            raise RuntimeError(
                f"S3 upload verification failed: size mismatch for {s3_key}\n"
                f"  Local: {local_size} bytes, Remote: {remote_size} bytes"
            )

        print(f"Verified S3 upload: {s3_key} ({remote_size / (1024 ** 2):.1f} MB)", flush=True)
    except Exception as e:
        raise RuntimeError(f"Failed to verify S3 upload for {s3_key}: {str(e)}") from e


def download_source(
    s3: Any,
    bucket: str,
    input_key: str,
    local_path: Path,
    heartbeat: Heartbeat | None = None,
) -> None:
    """
    Download source file from S3 with verification.

    Args:
        s3: boto3 S3 client
        bucket: S3 bucket name
        input_key: S3 object key
        local_path: Local path to save file

    Raises:
        RuntimeError: If download or verification fails
    """
    print(f"Downloading s3://{bucket}/{input_key} -> {local_path}", flush=True)
    size = int(s3.head_object(Bucket=bucket, Key=input_key)["ContentLength"])
    s3.download_file(
        bucket,
        input_key,
        str(local_path),
        Callback=_transfer_callback(heartbeat, "acquire", size),
    )
    verify_download(local_path)


def upload_file(
    s3: Any,
    bucket: str,
    s3_key: str,
    local_path: Path,
    heartbeat: Heartbeat | None = None,
) -> dict[str, Any]:
    """
    Upload file to S3 with verification and a recorded sha256 checksum.

    Args:
        s3: boto3 S3 client
        bucket: S3 bucket name
        s3_key: S3 object key
        local_path: Path to local file

    Returns:
        Dict with file, key, sha256, and size_bytes for the result payload
        (publisher verifies server-side against these before promotion)

    Raises:
        RuntimeError: If upload or verification fails
    """
    digest = sha256_file(local_path)
    print(f"Uploading {local_path} -> s3://{bucket}/{s3_key} (sha256={digest[:12]}...)",
          flush=True)
    s3.upload_file(
        str(local_path),
        bucket,
        s3_key,
        Callback=_transfer_callback(
            heartbeat, f"upload: {local_path.name}", local_path.stat().st_size
        ),
    )
    verify_upload(s3, bucket, s3_key, local_path)
    return {
        "file": local_path.name,
        "key": s3_key,
        "sha256": digest,
        "size_bytes": local_path.stat().st_size,
    }


def upload_outputs(
    s3: Any,
    bucket: str,
    output_prefix: str,
    files: list[tuple[Path, str]],
) -> list[dict[str, Any]]:
    """
    Upload multiple output files to S3.

    Args:
        s3: boto3 S3 client
        bucket: S3 bucket name
        output_prefix: S3 prefix for outputs (e.g. tracks/<id>/<gen>/staging/)
        files: List of (local_path, relative_key) tuples

    Returns:
        List of upload records with file, key, sha256, size_bytes

    Raises:
        RuntimeError: If any upload fails
    """
    uploads = []
    for local_path, relative_key in files:
        s3_key = f"{output_prefix}{relative_key}"
        upload_record = upload_file(s3, bucket, s3_key, local_path)
        upload_record["file"] = relative_key  # Use relative path in record
        uploads.append(upload_record)

    print(f"Successfully uploaded {len(uploads)} files", flush=True)
    return uploads
