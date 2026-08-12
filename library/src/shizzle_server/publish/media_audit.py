"""Local-file media inspection used by the VPS library auditor and publisher gates."""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .delivery_profile import (
    ProfileIssue,
    evaluate_audio_probe,
    evaluate_video_probe,
    has_errors,
)

HASH_CHUNK_BYTES = 8 * 1024 * 1024


class MediaAuditError(RuntimeError):
    """The inspection tool could not produce trustworthy evidence."""


@dataclass(frozen=True)
class AtomLayout:
    moov_offset: int | None
    mdat_offset: int | None
    atoms: tuple[str, ...]

    @property
    def fast_start(self) -> bool:
        return (
            self.moov_offset is not None
            and self.mdat_offset is not None
            and self.moov_offset < self.mdat_offset
        )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(HASH_CHUNK_BYTES), b""):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_mp4_atoms(path: Path) -> AtomLayout:
    """Read top-level ISO BMFF atoms without loading the object into memory."""
    file_size = path.stat().st_size
    offset = 0
    moov: int | None = None
    mdat: int | None = None
    atoms: list[str] = []
    with path.open("rb") as handle:
        while offset + 8 <= file_size:
            handle.seek(offset)
            header = handle.read(16)
            if len(header) < 8:
                break
            size = int.from_bytes(header[0:4], "big")
            atom_type = header[4:8].decode("latin-1")
            header_size = 8
            if size == 1:
                if len(header) < 16:
                    break
                size = int.from_bytes(header[8:16], "big")
                header_size = 16
            elif size == 0:
                size = file_size - offset
            if size < header_size or offset + size > file_size:
                raise MediaAuditError(
                    f"invalid MP4 atom {atom_type!r} at {offset}: size={size}, file={file_size}"
                )
            atoms.append(atom_type)
            if atom_type == "moov" and moov is None:
                moov = offset
            elif atom_type == "mdat" and mdat is None:
                mdat = offset
            offset += size
    return AtomLayout(moov_offset=moov, mdat_offset=mdat, atoms=tuple(atoms))


def _run(command: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise MediaAuditError(f"required executable not found: {command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaAuditError(f"command timed out after {timeout}s: {command[0]}") from exc


def ffprobe(path: Path, *, timeout: int = 120) -> dict[str, Any]:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise MediaAuditError(f"ffprobe failed for {path.name}: {result.stderr.strip()[:1000]}")
    try:
        payload: dict[str, Any] = json.loads(result.stdout)
        return payload
    except json.JSONDecodeError as exc:
        raise MediaAuditError(f"ffprobe returned invalid JSON for {path.name}") from exc


def full_decode(path: Path, *, timeout: int = 1800) -> tuple[bool, str]:
    """Decode every stream through EOF; `-xerror` makes corruption fatal."""
    result = _run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        timeout=timeout,
    )
    detail = result.stderr.strip()
    return result.returncode == 0, detail[:4000]


def keyframe_times(path: Path, *, timeout: int = 300) -> list[float]:
    result = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-skip_frame",
            "nokey",
            "-show_entries",
            "frame=best_effort_timestamp_time",
            "-of",
            "csv=p=0",
            str(path),
        ],
        timeout=timeout,
    )
    if result.returncode != 0:
        raise MediaAuditError(
            f"keyframe probe failed for {path.name}: {result.stderr.strip()[:1000]}"
        )
    times: list[float] = []
    for line in result.stdout.splitlines():
        value = line.strip().split(",", 1)[0]
        if not value or value == "N/A":
            continue
        try:
            times.append(float(value))
        except ValueError:
            continue
    return times


def max_keyframe_interval(times: list[float], duration: float) -> float | None:
    if not times:
        return None
    points = [0.0, *times, duration]
    return max(max(0.0, right - left) for left, right in zip(points, points[1:], strict=False))


def _stream(probe: dict[str, Any], kind: str) -> dict[str, Any] | None:
    return next((item for item in probe.get("streams", []) if item.get("codec_type") == kind), None)


def audit_audio_file(
    path: Path,
    *,
    artifact: str,
    expected_duration: float,
    preserve_existing_lossy: bool,
    decode: bool = True,
) -> dict[str, Any]:
    probe = ffprobe(path)
    stream = _stream(probe, "audio")
    issues: list[ProfileIssue] = []
    if stream is None:
        issues.append(ProfileIssue("audio-stream-missing", "no audio stream found", artifact))
    layout = inspect_mp4_atoms(path)
    if stream is not None:
        issues.extend(
            evaluate_audio_probe(
                stream,
                probe.get("format", {}),
                artifact=artifact,
                expected_duration=expected_duration,
                fast_start=layout.fast_start,
                preserve_existing_lossy=preserve_existing_lossy,
            )
        )
    decode_ok, decode_detail = full_decode(path) if decode else (False, "not run")
    if decode and not decode_ok:
        issues.append(ProfileIssue("audio-full-decode", decode_detail or "full decode failed", artifact))
    return {
        "artifact": artifact,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "probe": probe,
        "atoms": list(layout.atoms),
        "moov_offset": layout.moov_offset,
        "mdat_offset": layout.mdat_offset,
        "fast_start": layout.fast_start,
        "full_decode": "pass" if decode_ok else ("not-run" if not decode else "fail"),
        "decode_detail": decode_detail or None,
        "issues": [issue.as_dict() for issue in issues],
        "passed": not has_errors(issues),
    }


def audit_video_file(
    path: Path,
    *,
    artifact: str,
    expected_duration: float,
    decode: bool = True,
) -> dict[str, Any]:
    probe = ffprobe(path)
    stream = _stream(probe, "video")
    issues: list[ProfileIssue] = []
    layout = inspect_mp4_atoms(path)
    duration_raw = (stream or {}).get("duration") or probe.get("format", {}).get("duration")
    try:
        duration = float(duration_raw)
    except (TypeError, ValueError):
        duration = expected_duration
    times = keyframe_times(path)
    keyframe_interval = max_keyframe_interval(times, duration)
    if stream is None:
        issues.append(ProfileIssue("video-stream-missing", "no video stream found", artifact))
    else:
        audio_count = sum(
            1 for item in probe.get("streams", []) if item.get("codec_type") == "audio"
        )
        issues.extend(
            evaluate_video_probe(
                stream,
                probe.get("format", {}),
                artifact=artifact,
                expected_duration=expected_duration,
                fast_start=layout.fast_start,
                max_keyframe_interval_sec=keyframe_interval,
                audio_stream_count=audio_count,
            )
        )
    decode_ok, decode_detail = full_decode(path) if decode else (False, "not run")
    if decode and not decode_ok:
        issues.append(ProfileIssue("video-full-decode", decode_detail or "full decode failed", artifact))
    return {
        "artifact": artifact,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
        "probe": probe,
        "atoms": list(layout.atoms),
        "moov_offset": layout.moov_offset,
        "mdat_offset": layout.mdat_offset,
        "fast_start": layout.fast_start,
        "keyframe_count": len(times),
        "max_keyframe_interval_seconds": keyframe_interval,
        "full_decode": "pass" if decode_ok else ("not-run" if not decode else "fail"),
        "decode_detail": decode_detail or None,
        "issues": [issue.as_dict() for issue in issues],
        "passed": not has_errors(issues),
    }
