"""Consume a ``lossless-stem-v1`` package and publish a browser generation.

This is the VPS side of the interface: the one fixed transformation
(``interfaces/shizzle-browser-v1/spec.md``) applied to every accepted
package, with no song-specific alternate path.

    package (six float32 WAVs + handoff.json)  +  source video
        -> verify package (hashes, format, alignment)
        -> measure default-mix true peak -> one common ``default_gain_db``
        -> six AAC-LC/M4A stems at the 256 kb/s encoder target
        -> browser-safe audio-less H.264 video
        -> stage to S3, publish immutably (manifest last), activate DB row

Usage (also the VM proving path — MinIO is just AWS_ENDPOINT_URL):

    uv run --directory library python -m shizzle_server.publish.lossless_intake \
        --package /path/to/package --source /path/to/source.mp4 \
        --bucket karaoke-pimpshizzle --title "Black Hole Sun" --artist "Peter Frampton" \
        [--database-url postgresql+asyncpg://...] [--generation 1] [--track-id UUID]
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import re
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .delivery_profile import CANONICAL_STEM_IDS as STEM_ROLES
from .delivery_profile import TRACK_DURATION_TOLERANCE_SEC
from .publisher import (
    Publisher,
    StagedObject,
    generation_prefix,
    manifest_key,
    staging_prefix,
)

logger = logging.getLogger("lossless_intake")

INTERFACE = "lossless-stem-v1"
SAMPLE_RATE = 44100
TRUE_PEAK_CEILING_DBTP = -1.0
TRACK_BUDGET_BPS = 2_500_000

STEM_DISPLAY_NAMES = {
    "vocals": "Vocals", "drums": "Drums", "bass": "Bass",
    "guitar": "Guitar", "piano": "Piano", "shizzle": "Shizzle",
}


class IntakeError(RuntimeError):
    """A package failed verification or the transform failed a gate."""


class PackageNotReady(IntakeError):
    """The worker has not written its handoff-last completion marker yet."""


# --- 0. fetch the package across the interface --------------------------------


def _head_or_none(s3: Any, bucket: str, key: str) -> dict[str, Any] | None:
    try:
        head: dict[str, Any] = s3.head_object(Bucket=bucket, Key=key)
        return head
    except Exception as exc:
        resp = getattr(exc, "response", None)
        if isinstance(resp, dict) and (
            str(resp.get("Error", {}).get("Code", "")) in ("404", "NoSuchKey", "NotFound")
            or resp.get("ResponseMetadata", {}).get("HTTPStatusCode") == 404
        ):
            return None
        raise


def _download_object(
    s3: Any, bucket: str, key: str, dest: Path, expected_size: int | None = None
) -> None:
    """Fetch one object, skipping when the local file already matches in size."""
    dest = Path(dest)
    if (
        expected_size is not None
        and dest.exists()
        and dest.stat().st_size == expected_size
    ):
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    s3.download_file(bucket, key, str(dest))


def download_package(s3: Any, bucket: str, package_prefix: str, dest: Path) -> Path:
    """Pull a ``lossless-stem-v1`` package from S3 into ``dest``.

    The interface contract: ``handoff.json`` arrives at ``{prefix}/handoff.json``
    only once the worker has finished writing every stem. Its absence means the
    package has not crossed the interface yet — we refuse to read a half-written
    prefix. Stems are downloaded from the paths declared in the handoff so the
    worker's key layout stays its own concern. Idempotent: files already on
    disk with a matching size are skipped, so re-runs after a crash cost only
    the handoff read.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)

    handoff_key = f"{package_prefix}/handoff.json"
    head = _head_or_none(s3, bucket, handoff_key)
    if head is None:
        raise PackageNotReady("package has not crossed the interface")
    handoff_dest = dest / "handoff.json"
    _download_object(
        s3, bucket, handoff_key, handoff_dest, expected_size=int(head.get("ContentLength", -1))
    )

    try:
        handoff = json.loads(handoff_dest.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise IntakeError(f"invalid handoff.json: {exc}") from exc
    if not isinstance(handoff, dict) or not isinstance(handoff.get("stems"), list):
        raise IntakeError("handoff.json must contain a stems list")
    root = dest.resolve()
    for entry in handoff["stems"]:
        if not isinstance(entry, dict) or "file" not in entry:
            raise IntakeError("each handoff stem must declare a file")
        rel = str(entry["file"]).lstrip("/")
        target = (dest / rel).resolve()
        if not target.is_relative_to(root):
            raise IntakeError(f"stem path escapes package directory: {rel}")
        size = entry.get("bytes")
        if not isinstance(size, int) or size < 1:
            raise IntakeError(f"invalid stem size for {rel}")
        _download_object(
            s3, bucket, f"{package_prefix}/{rel}", target, expected_size=size
        )
    logger.info("package downloaded into %s (%d stems)", dest, len(handoff.get("stems", [])))
    return dest


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _run(cmd: list[str], timeout: int = 1800) -> subprocess.CompletedProcess[str]:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise IntakeError(f"{cmd[0]} failed: {proc.stderr[-400:]}")
    return proc


# --- 1. verify the package ----------------------------------------------------

@dataclass
class Package:
    root: Path
    handoff: dict[str, Any]

    @property
    def duration_seconds(self) -> float:
        return float(self.handoff["separation"]["sample_count"]) / SAMPLE_RATE


def _package_path(root: Path, declared: str) -> Path:
    target = (root / declared).resolve()
    if not target.is_relative_to(root.resolve()):
        raise IntakeError(f"stem path escapes package directory: {declared}")
    return target


def load_and_verify_package(root: Path) -> Package:
    """Every claim in handoff.json is re-proven against the actual files."""
    try:
        handoff = json.loads((root / "handoff.json").read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise IntakeError(f"invalid handoff.json: {exc}") from exc
    if not isinstance(handoff, dict) or handoff.get("interface") != INTERFACE:
        actual = handoff.get("interface") if isinstance(handoff, dict) else type(handoff).__name__
        raise IntakeError(f"not a {INTERFACE} package: {actual!r}")
    sep = handoff.get("separation")
    raw_stems = handoff.get("stems")
    source = handoff.get("source")
    if not isinstance(sep, dict) or not isinstance(raw_stems, list) or not isinstance(source, dict):
        raise IntakeError("handoff separation, source, and stems must be objects/list")
    if (
        not isinstance(source.get("object_key"), str)
        or not source["object_key"]
        or not isinstance(source.get("sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", source["sha256"]) is None
    ):
        raise IntakeError("source must declare an object key and lowercase sha256")
    for field in ("model_version", "worker_image"):
        if not isinstance(sep.get(field), str) or not sep[field]:
            raise IntakeError(f"separation.{field} must be a non-empty string")
    for field, want in (("model", "htdemucs_6s"),
                        ("sample_rate_hz", SAMPLE_RATE), ("channels", 2),
                        ("sample_format", "f32le"), ("start_sample", 0)):
        if sep.get(field) != want:
            raise IntakeError(f"separation.{field}={sep.get(field)!r}, expected {want!r}")
    sample_count = sep.get("sample_count")
    if not isinstance(sample_count, int) or sample_count < 1:
        raise IntakeError("separation.sample_count must be a positive integer")

    if len(raw_stems) != len(STEM_ROLES) or not all(isinstance(s, dict) for s in raw_stems):
        raise IntakeError(f"package must declare exactly {len(STEM_ROLES)} stem objects")
    stems: dict[str, dict[str, Any]] = {}
    for expected_role, entry in zip(STEM_ROLES, raw_stems, strict=True):
        role = entry.get("role")
        expected_file = f"stems/{expected_role}.wav"
        if role != expected_role or entry.get("file") != expected_file or role in stems:
            raise IntakeError(
                f"stem must map {expected_role!r} to {expected_file!r}, got "
                f"{role!r} -> {entry.get('file')!r}"
            )
        stems[role] = entry

    for role, entry in stems.items():
        path = _package_path(root, str(entry["file"]))
        if not path.exists():
            raise IntakeError(f"missing stem file: {entry['file']}")
        if path.stat().st_size != entry.get("bytes"):
            raise IntakeError(f"{role}: size {path.stat().st_size} != manifest {entry.get('bytes')}")
        if _sha256(path) != entry.get("sha256"):
            raise IntakeError(f"{role}: sha256 mismatch")
        try:
            probe = json.loads(_run([
                "ffprobe", "-v", "error", "-select_streams", "a:0",
                "-show_entries", "stream=codec_name,sample_rate,channels,duration_ts",
                "-of", "json", str(path),
            ]).stdout)["streams"][0]
            audio_format = (
                probe["codec_name"], int(probe["sample_rate"]), probe["channels"]
            )
            actual_samples = int(probe["duration_ts"])
        except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise IntakeError(f"{role}: invalid ffprobe result") from exc
        if audio_format != ("pcm_f32le", SAMPLE_RATE, 2):
            raise IntakeError(f"{role}: not stereo 44.1 kHz float32 ({probe})")
        if actual_samples != sample_count:
            raise IntakeError(
                f"{role}: sample count {actual_samples} != handoff {sample_count}"
            )
    logger.info("package verified: 6 stems, %.2f s", sample_count / SAMPLE_RATE)
    return Package(root=root, handoff=handoff)


# --- 2. one common gain -------------------------------------------------------

def measure_default_mix_true_peak(pkg: Package) -> float:
    """True peak (dBTP) of the unity default mix of all six stems."""
    cmd = ["ffmpeg", "-v", "info"]
    for role in STEM_ROLES:
        cmd += ["-i", str(pkg.root / "stems" / f"{role}.wav")]
    cmd += [
        "-filter_complex", "amix=inputs=6:normalize=0,ebur128=peak=true",
        "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    match = None
    for m in re.finditer(r"Peak:\s*(-?[\d.]+|-inf)\s*dBFS", proc.stderr):
        match = m  # the summary block is last
    if match is None:
        raise IntakeError(f"could not parse true peak from ebur128: {proc.stderr[-300:]}")
    return float("-inf") if match.group(1) == "-inf" else float(match.group(1))


def common_gain_db(true_peak_dbtp: float) -> float:
    """At most one common attenuation; never per-stem, never a boost."""
    if true_peak_dbtp <= TRUE_PEAK_CEILING_DBTP:
        return 0.0
    return math.floor((TRUE_PEAK_CEILING_DBTP - true_peak_dbtp) * 10) / 10


# --- 3. the fixed derivations (reference commands from shizzle-browser-v1) ----

def encode_stem(wav: Path, m4a: Path) -> None:
    _run([
        "ffmpeg", "-y", "-i", str(wav), "-map", "0:a:0",
        "-c:a", "aac", "-profile:a", "aac_low", "-b:a", "256k",
        "-ar", str(SAMPLE_RATE), "-ac", "2", "-movflags", "+faststart",
        str(m4a),
    ])


def derive_video(
    source: Path, out: Path, duration: float, maxrate_kbps: int = 1200
) -> None:
    _run([
        "ffmpeg", "-y", "-fflags", "+genpts", "-i", str(source),
        "-map", "0:v:0", "-an", "-t", str(duration),
        "-vf",
        "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:"
        "force_divisible_by=2,fps=30,format=yuv420p,setpts=PTS-STARTPTS",
        "-c:v", "libx264", "-preset", "medium", "-crf", "20",
        "-maxrate", f"{maxrate_kbps}k", "-bufsize", f"{2 * maxrate_kbps}k",
        "-profile:v", "main", "-level:v", "3.1",
        "-g", "60", "-keyint_min", "60", "-sc_threshold", "0",
        "-movflags", "+faststart", "-video_track_timescale", "90000",
        str(out),
    ])
    probe = _run([
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(out),
    ]).stdout.strip()
    try:
        actual = float(probe)
    except ValueError as exc:
        raise IntakeError(f"could not read derived video duration: {probe!r}") from exc
    delta = actual - duration
    if abs(delta) > TRACK_DURATION_TOLERANCE_SEC:
        raise IntakeError(
            f"video duration {actual:.3f}s is outside the stem timeline {duration:.3f}s"
        )


# --- 4. transform: package -> candidate generation on disk --------------------

def transform(pkg: Package, source_video: Path, candidate: Path,
              title: str, artist: str) -> dict[str, Any]:
    """Run the fixed transformation; return the v3 delivery manifest."""
    (candidate / "stems").mkdir(parents=True, exist_ok=True)

    tp = measure_default_mix_true_peak(pkg)
    gain = common_gain_db(tp)
    logger.info("default-mix true peak %.1f dBTP -> default_gain_db %.1f", tp, gain)

    for role in STEM_ROLES:
        logger.info("encode %s.m4a", role)
        encode_stem(pkg.root / "stems" / f"{role}.wav", candidate / "stems" / f"{role}.m4a")
    duration = pkg.duration_seconds
    logger.info("derive video.mp4")
    derive_video(source_video, candidate / "video.mp4", duration)

    def _avg_bps() -> float:
        total = sum(p.stat().st_size for p in candidate.rglob("*") if p.is_file())
        return total * 8 / duration

    if _avg_bps() > TRACK_BUDGET_BPS:
        # Bounded video re-derivation (shizzle-browser-v1): audio is fixed at
        # its 256k target, so the video is the adjustable component. Give it
        # whatever the budget leaves after audio, less 2% headroom for
        # container overhead, floored for basic watchability.
        audio_bps = sum(
            (candidate / "stems" / f"{r}.m4a").stat().st_size for r in STEM_ROLES
        ) * 8 / duration
        allowance_kbps = max(300, int((TRACK_BUDGET_BPS * 0.98 - audio_bps) / 1000))
        logger.info(
            "over budget at %.3f Mb/s; re-deriving video at maxrate %dk "
            "(audio measures %.3f Mb/s)",
            _avg_bps() / 1e6, allowance_kbps, audio_bps / 1e6,
        )
        derive_video(
            source_video, candidate / "video.mp4", duration,
            maxrate_kbps=allowance_kbps,
        )

    avg_bps = _avg_bps()
    if avg_bps > TRACK_BUDGET_BPS:
        raise IntakeError(
            f"complete generation {avg_bps/1e6:.3f} Mb/s exceeds the "
            f"{TRACK_BUDGET_BPS/1e6:.1f} Mb/s budget even after bounded "
            "video re-derivation"
        )
    logger.info("generation average %.3f Mb/s (budget %.1f)", avg_bps / 1e6, TRACK_BUDGET_BPS / 1e6)

    return {
        "version": 3,
        "title": title,
        "artist": artist,
        "duration": duration,
        "video": "video.mp4",
        "sourceUrl": "",
        "videoId": "",
        "video_meta": {},
        "stems": [
            {
                "id": role,
                "name": STEM_DISPLAY_NAMES[role],
                "file": f"stems/{role}.m4a",
                "default_gain_db": gain,
            }
            for role in STEM_ROLES
        ],
        "timeline": {
            "start_ms": 0,
            "duration_ms": int(round(duration * 1000)),
            "sample_rate_hz": SAMPLE_RATE,
        },
        "integrity": {
            "source": INTERFACE,
            "handoff_source_sha256": pkg.handoff["source"]["sha256"],
            "worker_image": pkg.handoff["separation"]["worker_image"],
            "default_mix_true_peak_dbtp": tp if math.isfinite(tp) else None,
        },
        "processing": {"source": INTERFACE, "origin": pkg.handoff["source"]["object_key"]},
    }


# --- 5. stage, publish, activate ----------------------------------------------

def stage(s3: Any, bucket: str, track_id: uuid.UUID, generation: int,
          candidate: Path, manifest: dict[str, Any]) -> list[StagedObject]:
    prefix = staging_prefix(track_id, generation)
    staged: list[StagedObject] = []
    files = sorted(
        p for p in candidate.rglob("*")
        if p.is_file() and p.name != "manifest.json"
    )
    for path in files:
        rel = path.relative_to(candidate).as_posix()
        content_type = "audio/mp4" if rel.endswith(".m4a") else "video/mp4"
        s3.upload_file(str(path), bucket, f"{prefix}{rel}",
                       ExtraArgs={"ContentType": content_type})
        staged.append(StagedObject(file=rel, size_bytes=path.stat().st_size,
                                   sha256=_sha256(path)))
        logger.info("staged %s (%.1f MiB)", rel, path.stat().st_size / 1024**2)
    mpath = candidate / "manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, allow_nan=False))
    s3.upload_file(str(mpath), bucket, f"{prefix}manifest.json",
                   ExtraArgs={"ContentType": "application/json"})
    staged.append(StagedObject(file="manifest.json", size_bytes=mpath.stat().st_size,
                               sha256=_sha256(mpath)))
    return staged


async def activate(database_url: str, track_id: uuid.UUID, generation: int,
                   manifest: dict[str, Any]) -> bool:
    from ..db import create_engine, create_session_factory
    from ..db.repository import TrackRepository

    engine = create_engine(database_url)
    try:
        repo = TrackRepository(create_session_factory(engine))
        _, created = await repo.upsert_imported(
            track_id,
            title=manifest["title"],
            artist=manifest["artist"],
            duration_seconds=float(manifest["duration"]),
            s3_prefix=generation_prefix(track_id, generation).rstrip("/"),
            manifest_key=manifest_key(track_id, generation),
            generation=generation,
            integrity=manifest["integrity"],
        )
        return created
    finally:
        await engine.dispose()


# --- CLI ----------------------------------------------------------------------

def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--package", required=True, type=Path)
    ap.add_argument("--source", required=True, type=Path, help="original source video")
    ap.add_argument("--bucket", required=True)
    ap.add_argument("--title", required=True)
    ap.add_argument("--artist", default="")
    ap.add_argument("--track-id", type=uuid.UUID, default=None)
    ap.add_argument("--generation", type=int, default=1)
    ap.add_argument("--database-url", default=None)
    ap.add_argument("--workdir", type=Path, default=None)
    args = ap.parse_args()

    import boto3

    track_id = args.track_id or uuid.uuid4()
    candidate = (args.workdir or args.package.parent) / f"candidate-{track_id}"

    pkg = load_and_verify_package(args.package)
    manifest = transform(pkg, args.source, candidate, args.title, args.artist)

    s3 = boto3.client("s3")  # region/endpoint/credentials from the environment
    staged = stage(s3, args.bucket, track_id, args.generation, candidate, manifest)
    publisher = Publisher(s3, args.bucket)
    result = publisher.publish(track_id, args.generation, staged)
    logger.info("published %s (%d objects, %.1f MiB)", result.s3_prefix,
                len(result.copied_keys), result.bytes_copied / 1024**2)

    if args.database_url:
        created = asyncio.run(activate(args.database_url, track_id, args.generation, manifest))
        logger.info("track row %s: %s", track_id, "created" if created else "updated")
    print(json.dumps({"track_id": str(track_id), "generation": args.generation,
                      "s3_prefix": result.s3_prefix, "manifest_key": result.manifest_key}))


if __name__ == "__main__":
    main()
