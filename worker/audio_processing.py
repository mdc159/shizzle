"""
Audio processing for Runpod Demucs handler.

Handles FFmpeg extraction and Demucs stem separation.
"""

import json
import subprocess
from pathlib import Path

# Stem order per PRD specification
STEM_ORDER = ["vocals", "drums", "bass", "guitar", "piano", "other"]

# Display names for stems
STEM_DISPLAY_NAMES = {
    "vocals": "Vocals",
    "drums": "Drums",
    "bass": "Bass",
    "guitar": "Guitar",
    "piano": "Piano",
    "other": "Shizzle",  # Renamed from "Ambience" - the pimpshizzle touch!
}


def _run(cmd: list[str], capture: bool = False) -> str | None:
    """
    Run command with logging.

    Args:
        cmd: Command and arguments
        capture: If True, capture and return stdout

    Returns:
        stdout if capture=True, else None

    Raises:
        subprocess.CalledProcessError: If command fails
    """
    print("RUN:", " ".join(cmd), flush=True)
    if capture:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return result.stdout
    subprocess.run(cmd, check=True)
    return None


def get_duration(video_path: Path) -> float:
    """
    Get video duration in seconds using ffprobe.

    Args:
        video_path: Path to video file

    Returns:
        Duration in seconds
    """
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path)
    ]
    output = _run(cmd, capture=True)
    data = json.loads(output)
    return float(data.get("format", {}).get("duration", 0))


def extract_audio(source_mp4: Path, output_wav: Path) -> None:
    """
    Extract audio from video to WAV format for Demucs.

    Args:
        source_mp4: Path to source video
        output_wav: Path for output WAV file
    """
    _run([
        "ffmpeg", "-y",
        "-i", str(source_mp4),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        str(output_wav)
    ])


def extract_video_only(source_mp4: Path, output_mp4: Path) -> None:
    """
    Extract video track without audio (for web mixer).

    Args:
        source_mp4: Path to source video
        output_mp4: Path for output video-only file
    """
    _run([
        "ffmpeg", "-y",
        "-i", str(source_mp4),
        "-an",
        "-c:v", "copy",
        str(output_mp4)
    ])


def run_demucs(audio_wav: Path, output_dir: Path, model: str) -> dict[str, Path]:
    """
    Run Demucs stem separation.

    Uses parameters from working Demucs-GUI config:
    - segment: 7s (max for htdemucs_6s is 7.8)
    - overlap: 0.25 (25% overlap between segments)
    - shifts: 0 (no random shifts for consistency)
    - clip-mode: rescale (prevents clipping)
    - int24 output (24-bit for quality)

    Args:
        audio_wav: Path to input audio WAV
        output_dir: Directory for Demucs output
        model: Demucs model name (e.g., htdemucs_6s)

    Returns:
        Dict mapping stem name to WAV path

    Raises:
        RuntimeError: If stems not found after separation
    """
    _run([
        "python3", "-m", "demucs",
        "-n", model,
        "--segment", "7",
        "--overlap", "0.25",
        "--shifts", "0",
        "--clip-mode", "rescale",
        "--int24",
        "--out", str(output_dir),
        str(audio_wav)
    ])

    # Locate demucs output folder
    track_dir = None
    for p in output_dir.rglob("audio"):
        if p.is_dir() and list(p.glob("*.wav")):
            track_dir = p
            break

    if not track_dir:
        raise RuntimeError("Could not find Demucs output folder containing stem WAV files.")

    # Verify all stems exist
    stem_wavs = {}
    for stem in STEM_ORDER:
        wav_path = track_dir / f"{stem}.wav"
        if not wav_path.exists():
            raise RuntimeError(f"Missing stem: {stem}.wav")
        stem_wavs[stem] = wav_path

    return stem_wavs


def create_multi_audio_mp4(
    video_only: Path,
    stem_wavs: dict[str, Path],
    output_path: Path,
) -> None:
    """
    Create canonical multi-audio MP4 with all stems as audio tracks.

    Args:
        video_only: Path to video-only MP4
        stem_wavs: Dict mapping stem name to WAV path
        output_path: Path for output multi-audio MP4
    """
    ffmpeg_cmd = [
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
        str(output_path)
    ]
    _run(ffmpeg_cmd)
