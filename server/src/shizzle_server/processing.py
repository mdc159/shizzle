"""
Pipeline: extract audio -> extract video -> demucs -> copy stems -> manifest -> multi-track MP4.

Reuses FFmpeg/Demucs commands from agent/demucs.py and runpod/audio_processing.py.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

logger = logging.getLogger(__name__)

# Ensure common tool directories are on PATH (Windows installs often miss these)
_EXTRA_PATH_DIRS = [r"C:\ffmpeg\bin"]
for _d in _EXTRA_PATH_DIRS:
    if Path(_d).is_dir() and _d not in os.environ.get("PATH", ""):
        os.environ["PATH"] = _d + os.pathsep + os.environ.get("PATH", "")

STEM_ORDER = ["vocals", "drums", "bass", "guitar", "piano", "other"]

STEM_DISPLAY_NAMES = {
    "vocals": "Vocals",
    "drums": "Drums",
    "bass": "Bass",
    "guitar": "Guitar",
    "piano": "Piano",
    "other": "Shizzle",
}

# Map stem names to StemId used by the UI (other -> shizzle)
STEM_ID_MAP = {
    "vocals": "vocals",
    "drums": "drums",
    "bass": "bass",
    "guitar": "guitar",
    "piano": "piano",
    "other": "shizzle",
}

CANONICAL_AUDIO_RATE = 48000

# Python executable for Demucs — use system python (not venv) since
# torch/demucs are installed globally or in a separate CUDA environment.
PYTHON = shutil.which("python3") or shutil.which("python") or "python3"


def check_dependencies() -> list[str]:
    """Check that ffmpeg, ffprobe, and demucs are available. Returns list of missing tools."""
    missing = []
    for tool in ["ffmpeg", "ffprobe"]:
        if shutil.which(tool) is None:
            missing.append(tool)

    # Check demucs via python -m demucs --help
    try:
        result = subprocess.run(
            [PYTHON, "-m", "demucs", "--help"],
            capture_output=True,
            timeout=10,
        )
        if result.returncode != 0:
            missing.append("demucs")
    except Exception:
        missing.append("demucs")

    return missing


def _run(cmd: list[str], timeout: int = 3600) -> subprocess.CompletedProcess[str]:
    """Run a subprocess command, logging and raising on failure."""
    logger.info("RUN: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if result.returncode != 0:
        logger.error("Command failed (rc=%d): %s", result.returncode, result.stderr[:500])
        raise RuntimeError(f"Command failed: {' '.join(cmd[:3])}... — {result.stderr[:300]}")
    return result


def extract_audio(video_path: Path, audio_path: Path) -> None:
    """Extract audio from video to WAV (PCM 16-bit, 48kHz, stereo)."""
    _run([
        "ffmpeg", "-y",
        "-i", str(video_path),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(CANONICAL_AUDIO_RATE),
        "-ac", "2",
        str(audio_path),
    ], timeout=300)


def extract_video_only(source_path: Path, output_path: Path) -> None:
    """
    Produce browser-safe video track without audio.

    Re-encoding to H.264/yuv420p avoids codec-compatibility issues from copy-mode
    inputs that can stall or fail to render in browsers.
    """
    _run([
        "ffmpeg", "-y",
        "-i", str(source_path),
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        str(output_path),
    ], timeout=1200)


def get_duration(video_path: Path) -> float:
    """Get media duration in seconds using ffprobe."""
    result = _run([
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
    ], timeout=30)
    data = json.loads(result.stdout)
    return float(data.get("format", {}).get("duration", 0))


def get_video_metadata(video_path: Path) -> dict[str, str | float]:
    """Extract lightweight video stream metadata for manifest diagnostics."""
    result = _run([
        "ffprobe", "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,profile,pix_fmt,avg_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
    ], timeout=30)
    data = json.loads(result.stdout)
    stream = (data.get("streams") or [{}])[0]
    return {
        "codec": stream.get("codec_name", ""),
        "profile": stream.get("profile", ""),
        "pix_fmt": stream.get("pix_fmt", ""),
        "avg_frame_rate": stream.get("avg_frame_rate", ""),
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
    }


def run_demucs(audio_path: Path, output_dir: Path) -> dict[str, Path]:
    """Run Demucs 6-stem separation. Returns dict mapping stem name to WAV path."""
    output_dir.mkdir(parents=True, exist_ok=True)

    _run([
        PYTHON, "-m", "demucs",
        "-n", "htdemucs_6s",
        "--segment", "7",
        "--overlap", "0.25",
        "--shifts", "0",
        "--clip-mode", "rescale",
        "--int24",
        "--out", str(output_dir),
        str(audio_path),
    ], timeout=3600)

    # Locate demucs output (creates nested model/trackname/ dirs)
    track_dir = None
    for p in output_dir.rglob("audio"):
        if p.is_dir() and list(p.glob("*.wav")):
            track_dir = p
            break
    if not track_dir:
        for p in output_dir.rglob("*.wav"):
            track_dir = p.parent
            break
    if not track_dir:
        raise RuntimeError("Could not find Demucs output containing stem WAV files")

    stem_wavs = {}
    for stem in STEM_ORDER:
        wav = track_dir / f"{stem}.wav"
        if not wav.exists():
            raise RuntimeError(f"Missing stem: {stem}.wav in {track_dir}")
        stem_wavs[stem] = wav

    return stem_wavs


def copy_stems(stem_wavs: dict[str, Path], stems_dir: Path) -> dict[str, Path]:
    """Copy stem WAVs to final stems/ directory."""
    stems_dir.mkdir(parents=True, exist_ok=True)
    final = {}
    for stem, src in stem_wavs.items():
        dst = stems_dir / f"{stem}.wav"
        shutil.copy2(src, dst)
        final[stem] = dst
    return final


def compress_stems(stem_wavs: dict[str, Path], stems_dir: Path) -> dict[str, Path]:
    """Compress WAV stems to AAC .m4a at 256kbps for browser playback."""
    compressed = {}
    for stem, wav_path in stem_wavs.items():
        m4a_path = stems_dir / f"{stem}.m4a"
        _run([
            "ffmpeg", "-y",
            "-i", str(wav_path),
            "-c:a", "aac", "-b:a", "256k",
            "-ar", str(CANONICAL_AUDIO_RATE),
            "-movflags", "+faststart",
            str(m4a_path),
        ], timeout=300)
        compressed[stem] = m4a_path
    return compressed


def write_manifest(
    stems_dir: Path,
    _stem_wavs: dict[str, Path],
    duration: float,
    title: str,
    video_meta: dict[str, str | float] | None = None,
) -> Path:
    """Write stems.json manifest matching StemsManifest type."""
    stems = []
    for stem in STEM_ORDER:
        # Use compressed .m4a if available, otherwise fall back to .wav
        m4a = stems_dir / f"{stem}.m4a"
        ext = "m4a" if m4a.exists() else "wav"
        stems.append({
            "id": STEM_ID_MAP[stem],
            "name": STEM_DISPLAY_NAMES[stem],
            "file": f"stems/{stem}.{ext}",
            "default_gain": 0,
        })

    manifest = {
        "version": 2,
        "title": title,
        "artist": "",
        "duration": duration,
        "video": "video.mp4",
        "timeline": {
            "start_ms": 0,
            "duration_ms": int(round(duration * 1000)),
            "sample_rate_hz": CANONICAL_AUDIO_RATE,
        },
        "video_meta": video_meta or {},
        "stems": stems,
    }

    manifest_path = stems_dir.parent / "stems.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))
    return manifest_path


def create_multi_track_mp4(
    video_only: Path,
    stem_wavs: dict[str, Path],
    output_path: Path,
) -> None:
    """Create MP4 with video + 6 AAC audio tracks (no original stereo)."""
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(video_only),
        "-i", str(stem_wavs["vocals"]),
        "-i", str(stem_wavs["drums"]),
        "-i", str(stem_wavs["bass"]),
        "-i", str(stem_wavs["guitar"]),
        "-i", str(stem_wavs["piano"]),
        "-i", str(stem_wavs["other"]),
        "-map", "0:v",
        "-map", "1:a", "-metadata:s:a:0", f"handler_name={STEM_DISPLAY_NAMES['vocals']}",
        "-map", "2:a", "-metadata:s:a:1", f"handler_name={STEM_DISPLAY_NAMES['drums']}",
        "-map", "3:a", "-metadata:s:a:2", f"handler_name={STEM_DISPLAY_NAMES['bass']}",
        "-map", "4:a", "-metadata:s:a:3", f"handler_name={STEM_DISPLAY_NAMES['guitar']}",
        "-map", "5:a", "-metadata:s:a:4", f"handler_name={STEM_DISPLAY_NAMES['piano']}",
        "-map", "6:a", "-metadata:s:a:5", f"handler_name={STEM_DISPLAY_NAMES['other']}",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "256k",
        "-movflags", "+faststart",
        "-disposition:a:0", "none",
        str(output_path),
    ]
    _run(cmd, timeout=600)


def run_pipeline(
    job_dir: Path,
    source_path: Path,
    on_status: Callable[[str, str], None],
    title: str | None = None,
) -> None:
    """
    Run the full processing pipeline.

    on_status(status, message) is called at each stage.
    """
    if not title:
        title = source_path.stem

    # 1. Extract audio
    on_status("extracting", "Extracting audio from video")
    audio_path = job_dir / "audio.wav"
    extract_audio(source_path, audio_path)

    # 2. Extract video only
    on_status("extracting", "Extracting video track")
    video_path = job_dir / "video.mp4"
    extract_video_only(source_path, video_path)

    # 3. Get duration
    duration = get_duration(video_path)
    video_meta = get_video_metadata(video_path)

    # 4. Run Demucs
    on_status("splitting", "Running Demucs stem separation (GPU)")
    demucs_out = job_dir / "demucs_out"
    stem_wavs = run_demucs(audio_path, demucs_out)

    # 5. Copy stems to final location
    on_status("encoding", "Copying stems")
    stems_dir = job_dir / "stems"
    final_stems = copy_stems(stem_wavs, stems_dir)

    # 6. Compress stems for browser playback (WAV → AAC .m4a)
    on_status("encoding", "Compressing stems for browser playback")
    compress_stems(final_stems, stems_dir)

    # 7. Write manifest (auto-detects .m4a if present)
    write_manifest(stems_dir, final_stems, duration, title, video_meta=video_meta)

    # 8. Create multi-track MP4
    on_status("encoding", "Creating multi-track MP4")
    multi_track = job_dir / "multi-track.mp4"
    create_multi_track_mp4(video_path, final_stems, multi_track)

    # 9. Cleanup temp files
    audio_path.unlink(missing_ok=True)
    shutil.rmtree(demucs_out, ignore_errors=True)

    on_status("ready", "Processing complete")
