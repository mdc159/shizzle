"""Lossless-stem worker: source object in, `lossless-stem-v1` package out.

Written clean against ``interfaces/lossless-stem-v1/schema.json``. This worker
owns separation and nothing after it: no AAC, no video, no delivery manifest.
Those belong to the finished VPS pipeline on the other side of the interface.

Pipeline (each phase heartbeats):

  acquire   -> source object on local disk (S3 download, or a local file)
  extract   -> stereo 44.1 kHz float32 WAV via ffmpeg
  separate  -> htdemucs_6s, six aligned float32 stem arrays
  write     -> stems/<role>.wav, sample zero, identical sample counts;
               the separator's native "other" is renamed "shizzle" here
  upload    -> all six WAVs to the separation prefix
  handoff   -> handoff.json written LAST; a package without it has not
               crossed the interface

Idempotent by construction: every write/upload overwrites, so a redelivered
job cleanly replaces a dead predecessor's partial outputs.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
import time
from collections.abc import Callable
from pathlib import Path

import numpy as np
import soundfile as sf

MODEL_NAME = "htdemucs_6s"
SAMPLE_RATE = 44100
CHANNELS = 2
INTERFACE = "lossless-stem-v1"

# Interface roles in package order. The separator emits "other" for the sixth
# source; this worker renames it at the write (design authority, Mike
# 2026-08-11) so one name runs the pipeline from handoff to browser.
ROLES = ("vocals", "drums", "bass", "guitar", "piano", "shizzle")
SEPARATOR_TO_ROLE = {
    "vocals": "vocals",
    "drums": "drums",
    "bass": "bass",
    "guitar": "guitar",
    "piano": "piano",
    "other": "shizzle",
}

Heartbeat = Callable[[str], None]


def _noop_heartbeat(_msg: str) -> None:
    pass


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def extract_audio(source: Path, wav_out: Path, heartbeat: Heartbeat = _noop_heartbeat) -> Path:
    """Decode the source's first audio stream to stereo 44.1 kHz float32 WAV."""
    heartbeat("extract: ffmpeg decode to 44.1 kHz stereo float32")
    cmd = [
        "ffmpeg", "-y", "-i", str(source),
        "-map", "0:a:0", "-vn",
        "-ac", str(CHANNELS), "-ar", str(SAMPLE_RATE),
        "-c:a", "pcm_f32le",
        str(wav_out),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"ffmpeg extract failed: {proc.stderr[-500:]}")
    return wav_out


def separate(
    audio_wav: Path,
    device: str | None = None,
    heartbeat: Heartbeat = _noop_heartbeat,
) -> tuple[dict[str, np.ndarray], str]:
    """Run htdemucs_6s. Returns ({role: float32 array (samples, 2)}, model_version)."""
    import torch
    from demucs.api import Separator

    resolved = device or ("cuda" if torch.cuda.is_available() else "cpu")
    heartbeat(f"separate: loading {MODEL_NAME} on {resolved}")
    separator = Separator(model=MODEL_NAME, device=resolved, progress=False)
    if separator.samplerate != SAMPLE_RATE:  # model property, not a knob
        raise RuntimeError(f"model samplerate {separator.samplerate} != {SAMPLE_RATE}")

    heartbeat("separate: running model")
    _origin, separated = separator.separate_audio_file(audio_wav)

    import demucs

    stems: dict[str, np.ndarray] = {}
    for name, tensor in separated.items():
        role = SEPARATOR_TO_ROLE.get(name)
        if role is None:
            raise RuntimeError(f"unexpected separator source: {name}")
        stems[role] = tensor.cpu().numpy().T.astype(np.float32, copy=False)

    missing = [r for r in ROLES if r not in stems]
    if missing:
        raise RuntimeError(f"separator did not produce: {missing}")
    lengths = {r: s.shape[0] for r, s in stems.items()}
    if len(set(lengths.values())) != 1:
        raise RuntimeError(f"stem sample counts differ: {lengths}")
    return stems, getattr(demucs, "__version__", "unknown")


def write_stems(
    stems: dict[str, np.ndarray],
    stems_dir: Path,
    heartbeat: Heartbeat = _noop_heartbeat,
) -> dict[str, Path]:
    """Write six float32 WAVs at their interface paths. Overwrites cleanly."""
    stems_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for i, role in enumerate(ROLES, 1):
        path = stems_dir / f"{role}.wav"
        heartbeat(f"write: {role}.wav ({i}/6)")
        sf.write(str(path), stems[role], SAMPLE_RATE, subtype="FLOAT")
        paths[role] = path
    return paths


def build_handoff(
    track_id: str,
    generation: int,
    source_key: str,
    source_sha256: str,
    model_version: str,
    worker_image: str,
    sample_count: int,
    stem_paths: dict[str, Path],
) -> dict:
    """Assemble the handoff.json document exactly per the interface schema."""
    return {
        "interface": INTERFACE,
        "track_id": track_id,
        "generation": generation,
        "source": {"object_key": source_key, "sha256": source_sha256},
        "separation": {
            "model": MODEL_NAME,
            "model_version": model_version,
            "worker_image": worker_image,
            "sample_rate_hz": SAMPLE_RATE,
            "channels": CHANNELS,
            "sample_format": "f32le",
            "start_sample": 0,
            "sample_count": sample_count,
        },
        "stems": [
            {
                "role": role,
                "file": f"stems/{role}.wav",
                "bytes": stem_paths[role].stat().st_size,
                "sha256": sha256_file(stem_paths[role]),
            }
            for role in ROLES
        ],
    }


def run(
    source: Path,
    out_dir: Path,
    *,
    track_id: str,
    generation: int,
    source_key: str,
    worker_image: str,
    device: str | None = None,
    heartbeat: Heartbeat = _noop_heartbeat,
) -> dict:
    """Source file -> complete package on local disk. Returns the handoff dict.

    Writes ``<out_dir>/stems/<role>.wav`` (six) and ``<out_dir>/handoff.json``
    LAST. Uploading is the caller's concern (S3 in the handler, nothing in
    local mode) so this core stays platform-free and directly testable.
    """
    timings: dict[str, float] = {}
    t0 = time.monotonic()
    heartbeat(f"acquire: source {source.name} ({source.stat().st_size} bytes)")
    source_sha = sha256_file(source)

    with tempfile.TemporaryDirectory(dir=out_dir) as tmp:
        audio = extract_audio(source, Path(tmp) / "audio.wav", heartbeat)
        timings["extract"] = round(time.monotonic() - t0, 2)

        t1 = time.monotonic()
        stems, model_version = separate(audio, device, heartbeat)
        timings["separate"] = round(time.monotonic() - t1, 2)

    t2 = time.monotonic()
    stem_paths = write_stems(stems, out_dir / "stems", heartbeat)
    timings["write"] = round(time.monotonic() - t2, 2)

    sample_count = next(iter(stems.values())).shape[0]
    handoff = build_handoff(
        track_id, generation, source_key, source_sha,
        model_version, worker_image, sample_count, stem_paths,
    )
    handoff["_timings"] = timings  # stripped before the final write by handler;
    # kept for local-mode visibility (schema forbids unknown top-level keys on
    # the uploaded document, not on this in-memory copy).
    heartbeat("package: six stems written; handoff pending upload")
    return handoff
