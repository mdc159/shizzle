"""Download executor: URL -> sources/<track>/source.mp4 + source.json (last).

The receipt (``shizzle-source-v1``) is the completion marker the orchestrator
re-proves (A12): object key, sha256 and size must match the uploaded object.
Nothing here touches the published library; only ``sources/<track>/`` is
written, which the node's IAM policy is scoped to.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SOURCE_INTERFACE = "shizzle-source-v1"
Progress = Callable[[str], None]


class DownloadError(RuntimeError):
    """A structured download failure; ``code`` is what the orchestrator records."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class SourceFile:
    path: Path
    bytes: int
    sha256: str
    duration_seconds: float
    title: str
    uploader: str
    video_id: str
    downloader: str


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def classify_ytdlp_error(message: str) -> str:
    lowered = message.lower()
    if "sign in to confirm" in lowered or "not a bot" in lowered or "cookies" in lowered:
        return "YOUTUBE_SIGN_IN_REQUIRED"
    if "video unavailable" in lowered or "private video" in lowered or "removed" in lowered:
        return "VIDEO_UNAVAILABLE"
    if "is live" in lowered or "live event" in lowered:
        return "LIVE_VIDEO"
    if "file is larger than max-filesize" in lowered or "max-filesize" in lowered:
        return "SOURCE_SIZE"
    return "DOWNLOAD_FAILED"


def probe(path: Path, *, ffprobe: str = "ffprobe") -> dict[str, Any]:
    """ffprobe JSON for the file; raises DownloadError(SOURCE_INVALID) when unusable."""
    try:
        completed = subprocess.run(
            [ffprobe, "-v", "error", "-show_format", "-show_streams", "-of", "json", str(path)],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DownloadError("SOURCE_INVALID", f"ffprobe failed: {exc.__class__.__name__}") from exc
    if completed.returncode != 0:
        raise DownloadError("SOURCE_INVALID", completed.stderr.strip()[-300:] or "ffprobe error")
    try:
        info = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise DownloadError("SOURCE_INVALID", "ffprobe returned no JSON") from exc
    return info if isinstance(info, dict) else {}


def validate_probe(info: dict[str, Any], *, max_duration_seconds: float) -> float:
    """Return the duration; the file must be an MP4 with one audio and one video stream."""
    fmt = info.get("format") or {}
    streams = info.get("streams") or []
    kinds = {str(s.get("codec_type")) for s in streams if isinstance(s, dict)}
    if not {"audio", "video"} <= kinds:
        raise DownloadError("SOURCE_INVALID", "source needs both an audio and a video stream")
    if "mp4" not in str(fmt.get("format_name", "")).split(","):
        raise DownloadError("SOURCE_INVALID", f"source is not MP4: {fmt.get('format_name')!r}")
    try:
        duration = float(fmt.get("duration"))
    except (TypeError, ValueError) as exc:
        raise DownloadError("SOURCE_INVALID", "source has no duration") from exc
    if not math.isfinite(duration) or duration <= 0:
        raise DownloadError("SOURCE_INVALID", "source duration is not positive")
    if duration > max_duration_seconds:
        raise DownloadError(
            "SOURCE_TOO_LONG",
            f"source is {duration:.0f}s, above the {max_duration_seconds:.0f}s limit",
        )
    return duration


def download_source(
    url: str,
    workdir: Path,
    *,
    max_bytes: int,
    max_duration_seconds: float,
    cookies_file: Path | None,
    progress: Progress,
) -> SourceFile:
    """yt-dlp the single video at ``url`` into ``workdir/source.mp4``."""
    try:
        import yt_dlp
        from yt_dlp.utils import DownloadError as YtDlpDownloadError
    except ImportError as exc:  # pragma: no cover - environment problem
        raise DownloadError("DOWNLOAD_FAILED", "yt-dlp is not installed") from exc

    workdir.mkdir(parents=True, exist_ok=True)
    for stale in workdir.glob("source.*"):
        stale.unlink()
    target = workdir / "source.mp4"
    last_bucket = [-1]

    def hook(status: dict[str, Any]) -> None:
        if status.get("status") != "downloading":
            return
        total = status.get("total_bytes") or status.get("total_bytes_estimate") or 0
        done = status.get("downloaded_bytes") or 0
        if total:
            bucket = min(20, int(20 * done / total))
            if bucket != last_bucket[0]:
                last_bucket[0] = bucket
                progress(f"download: {bucket * 5}%")

    options: dict[str, Any] = {
        # Prefer H.264 MP4 + AAC so the file is a real MP4 without a re-encode.
        "format": (
            "bestvideo[ext=mp4][vcodec^=avc1]+bestaudio[ext=m4a]"
            "/bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best"
        ),
        "merge_output_format": "mp4",
        "outtmpl": str(workdir / "source.%(ext)s"),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [hook],
        "max_filesize": max_bytes,
        "retries": 3,
        "fragment_retries": 3,
    }
    if cookies_file is not None:
        options["cookiefile"] = str(cookies_file)

    progress("download: starting")
    try:
        with yt_dlp.YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=True)
    except YtDlpDownloadError as exc:
        message = str(exc)
        raise DownloadError(classify_ytdlp_error(message), message[-400:]) from exc
    except Exception as exc:  # yt-dlp raises a zoo; keep the class name, not the URL
        raise DownloadError("DOWNLOAD_FAILED", exc.__class__.__name__) from exc

    produced = sorted(workdir.glob("source.*"))
    if not produced:
        raise DownloadError("DOWNLOAD_FAILED", "yt-dlp produced no file")
    if target not in produced:
        progress("download: remuxing to MP4")
        _to_mp4(produced[0], target)
    if target.stat().st_size > max_bytes:
        raise DownloadError("SOURCE_SIZE", f"source is {target.stat().st_size} bytes")

    progress("download: probing")
    duration = validate_probe(probe(target), max_duration_seconds=max_duration_seconds)
    progress("download: hashing")
    info = info if isinstance(info, dict) else {}
    return SourceFile(
        path=target,
        bytes=target.stat().st_size,
        sha256=sha256_file(target),
        duration_seconds=duration,
        title=str(info.get("title") or "")[:512],
        uploader=str(info.get("uploader") or info.get("channel") or "")[:256],
        video_id=str(info.get("id") or "")[:64],
        downloader=f"yt-dlp {getattr(yt_dlp.version, '__version__', 'unknown')}",
    )


def _to_mp4(source: Path, target: Path, *, ffmpeg: str = "ffmpeg") -> None:
    """Remux (or, failing that, transcode) a non-MP4 download into MP4."""
    if shutil.which(ffmpeg) is None:
        raise DownloadError("DOWNLOAD_FAILED", "ffmpeg is not on PATH to produce an MP4")
    copy = subprocess.run(
        [ffmpeg, "-y", "-i", str(source), "-c", "copy", "-movflags", "+faststart", str(target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if copy.returncode != 0:
        encode = subprocess.run(
            [ffmpeg, "-y", "-i", str(source), "-c:v", "libx264", "-preset", "veryfast",
             "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "256k",
             "-movflags", "+faststart", str(target)],
            capture_output=True,
            text=True,
            check=False,
        )
        if encode.returncode != 0:
            raise DownloadError("DOWNLOAD_FAILED", encode.stderr.strip()[-300:])
    source.unlink(missing_ok=True)


def build_receipt(
    source: SourceFile, *, payload: dict[str, Any], node_id: str, now: datetime | None = None
) -> dict[str, Any]:
    return {
        "interface": SOURCE_INTERFACE,
        "track_id": str(payload["track_id"]),
        "object_key": str(payload["source_key"]),
        "bytes": source.bytes,
        "sha256": source.sha256,
        "duration_seconds": round(source.duration_seconds, 3),
        "source_ref": str(payload.get("source_ref") or ""),
        "video_id": source.video_id,
        "title": source.title,
        "uploader": source.uploader,
        "downloader": source.downloader,
        "node_id": node_id,
        "downloaded_at": (now or datetime.now(UTC)).isoformat(),
    }


def upload_source(
    s3: Any,
    *,
    bucket: str,
    source: SourceFile,
    receipt: dict[str, Any],
    progress: Progress,
) -> None:
    """Upload source.mp4, verify its size, then write source.json LAST (A12).

    Idempotent: an existing source.json with the same sha256 means a previous
    run already finished and nothing is re-uploaded.
    """
    key = receipt["object_key"]
    receipt_key = f"sources/{receipt['track_id']}/source.json"
    existing = _read_json(s3, bucket, receipt_key)
    if existing is not None and existing.get("sha256") == source.sha256:
        progress("upload: already complete")
        return
    progress("upload: source.mp4")
    s3.upload_file(str(source.path), bucket, key, ExtraArgs={"ContentType": "video/mp4"})
    head = s3.head_object(Bucket=bucket, Key=key)
    if int(head.get("ContentLength", -1)) != source.bytes:
        raise DownloadError(
            "UPLOAD_FAILED", f"uploaded {head.get('ContentLength')} bytes, expected {source.bytes}"
        )
    progress("upload: source.json")
    s3.put_object(
        Bucket=bucket,
        Key=receipt_key,
        Body=json.dumps(receipt, indent=2, allow_nan=False).encode("utf-8"),
        ContentType="application/json",
    )


def _read_json(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception as exc:  # NoSuchKey and friends
        code = str(getattr(exc, "response", {}).get("Error", {}).get("Code", ""))
        if code in ("404", "NoSuchKey", "NotFound"):
            return None
        raise
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None
