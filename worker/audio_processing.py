"""
Audio/video processing for the Shizzle GPU worker.

Pipeline stages implemented here:
  - ffprobe duration / video metadata
  - canonical audio extraction (PCM s16le 48 kHz stereo)
  - browser-safe H.264 video re-encode (no -c:v copy; codec-compat issues
    from copy-mode inputs can stall or fail to render in browsers)
  - Demucs separation via the Python API with float32 preservation
    (demucs 4.0.1's CLI cannot do clip-mode 'none'; spike 0.3 showed
    --clip-mode rescale poisons relative stem levels, drums -1.24 dB)
  - common-gain policy: ONE gain applied identically to all stems, from
    max(stem peaks, unity-sum peak) with margin to 0.99 FS
  - AAC stem encode with a bitrate knob (STEM_AAC_BITRATE)
  - optional multi-track.mp4 mux (video + 6 AAC tracks)

torch/demucs are imported lazily so pure-logic tests run without them.
"""

import json
import math
import subprocess
from pathlib import Path

import numpy as np

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

# Map stem names to StemId used by the UI (other -> shizzle)
STEM_ID_MAP = {
    "vocals": "vocals",
    "drums": "drums",
    "bass": "bass",
    "guitar": "guitar",
    "piano": "piano",
    "other": "shizzle",
}

# Canonical extraction rate (matches server processing.py); Demucs itself
# consumes audio at its model rate (44100 for htdemucs_6s) via its own decode.
CANONICAL_AUDIO_RATE = 48000

# Demucs invocation parameters (semantics of the tuned CLI flags:
# --segment 7 --overlap 0.25 --shifts 0; segment 7 < 7.8 max for htdemucs_6s)
DEMUCS_SEGMENT = 7
DEMUCS_OVERLAP = 0.25
DEMUCS_SHIFTS = 0

# Common-gain policy target: peak headroom margin to full scale (~0.09 dB).
GAIN_TARGET_PEAK = 0.99


def _run(cmd: list[str], capture: bool = False) -> str | None:
    """Run command with logging. Raises subprocess.CalledProcessError on failure."""
    print("RUN:", " ".join(cmd), flush=True)
    if capture:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return result.stdout
    subprocess.run(cmd, check=True)
    return None


def get_duration(video_path: Path) -> float:
    """Get media duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
    ]
    output = _run(cmd, capture=True)
    data = json.loads(output or "{}")
    return float(data.get("format", {}).get("duration", 0))


def get_video_metadata(video_path: Path) -> dict[str, str | float]:
    """Extract lightweight video stream metadata for manifest diagnostics."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name,profile,pix_fmt,avg_frame_rate",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path),
    ]
    output = _run(cmd, capture=True)
    data = json.loads(output or "{}")
    stream = (data.get("streams") or [{}])[0]
    return {
        "codec": stream.get("codec_name", ""),
        "profile": stream.get("profile", ""),
        "pix_fmt": stream.get("pix_fmt", ""),
        "avg_frame_rate": stream.get("avg_frame_rate", ""),
        "duration": float(data.get("format", {}).get("duration", 0) or 0),
    }


def extract_audio(source_mp4: Path, output_wav: Path) -> None:
    """Extract canonical audio from video: PCM s16le, 48 kHz, stereo."""
    _run([
        "ffmpeg", "-y",
        "-i", str(source_mp4),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", str(CANONICAL_AUDIO_RATE),
        "-ac", "2",
        str(output_wav),
    ])


def extract_video_only(source_mp4: Path, output_mp4: Path) -> None:
    """
    Produce browser-safe video track without audio.

    Re-encoding to H.264/yuv420p (instead of -c:v copy) avoids
    codec-compatibility issues from copy-mode inputs that can stall or fail
    to render in browsers. Invocation lifted from server processing.py.
    """
    _run([
        "ffmpeg", "-y",
        "-i", str(source_mp4),
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "20",
        "-profile:v", "main",
        "-level:v", "3.1",
        "-pix_fmt", "yuv420p",
        "-vf", "setpts=PTS-STARTPTS",
        "-r", "30",
        "-g", "60",
        "-keyint_min", "60",
        "-sc_threshold", "0",
        "-movflags", "+faststart",
        "-video_track_timescale", "90000",
        str(output_mp4),
    ])


def decode_reference(audio_path: Path, output_wav: Path, sample_rate: int) -> None:
    """
    Decode the canonical audio to float32 at the model rate through the exact
    ffmpeg path Demucs uses internally (-ar <rate> f32le), so resampler
    differences are excluded from the integrity residual (spike 0.3 method).
    """
    _run([
        "ffmpeg", "-y",
        "-i", str(audio_path),
        "-ar", str(sample_rate),
        "-ac", "2",
        "-c:a", "pcm_f32le",
        str(output_wav),
    ])


def separate_stems(
    audio_path: Path, model_name: str
) -> tuple[dict[str, np.ndarray], int]:
    """
    Run Demucs separation via the Python API, preserving float32 output.

    Replicates demucs.separate.main() exactly (load path, normalization,
    apply_model parameters) but never applies per-stem clip handling —
    demucs 4.0.1's CLI only exposes --clip-mode {rescale,clamp}, and rescale
    silently alters relative stem levels (spike 0.3: drums -1.24 dB).

    Returns:
        (stems, sample_rate): stems maps stem name -> float32 ndarray of
        shape (samples, channels); sample_rate is the model rate (44100).
    """
    import torch
    from demucs.apply import apply_model
    from demucs.audio import AudioFile
    from demucs.pretrained import get_model

    device = "cuda" if torch.cuda.is_available() else "cpu"
    print(f"Demucs: model={model_name} device={device}", flush=True)

    model = get_model(model_name)
    model.cpu()
    model.eval()

    # demucs 4.1.0 dropped the module-level demucs.separate.load_track helper.
    # Replicate its primary load path exactly (AudioFile, the same ffmpeg-based
    # float32 decode demucs uses internally): first stream, resampled to the
    # model rate, remixed to the model channel count. Shape: (channels, samples).
    wav = AudioFile(audio_path).read(
        streams=0,
        samplerate=model.samplerate,
        channels=model.audio_channels,
    )
    ref = wav.mean(0)
    wav -= ref.mean()
    wav /= ref.std()
    sources = apply_model(
        model, wav[None],
        device=device,
        shifts=DEMUCS_SHIFTS,
        split=True,
        overlap=DEMUCS_OVERLAP,
        progress=True,
        num_workers=0,
        segment=DEMUCS_SEGMENT,
    )[0]
    sources *= ref.std()
    sources += ref.mean()

    if device == "cuda":
        print("cuda device used:", torch.cuda.get_device_name(0), flush=True)

    stems: dict[str, np.ndarray] = {}
    for source, name in zip(sources, model.sources, strict=True):
        # (channels, samples) tensor -> (samples, channels) float32 array
        stems[name] = source.cpu().numpy().T.astype(np.float32)

    missing = [s for s in STEM_ORDER if s not in stems]
    if missing:
        raise RuntimeError(f"Demucs did not produce expected stems: {missing}")

    return stems, int(model.samplerate)


def compute_common_gain(
    stems: dict[str, np.ndarray], target_peak: float = GAIN_TARGET_PEAK
) -> dict[str, float]:
    """
    Compute ONE common gain for all stems (never per-stem normalization).

    Chosen from max(all stem peaks, unity-sum peak) so that both individual
    stems and their unity sum stay below target_peak full scale. Applied
    identically to every stem, so relative levels are preserved exactly.
    Recorded in the manifest. Gain is never > 1.0 (attenuate only).

    Returns dict with linear gain, dB gain, and the peaks that drove it.
    """
    max_stem_peak = max(float(np.max(np.abs(x))) for x in stems.values())
    unity_sum = None
    for x in stems.values():
        unity_sum = x.astype(np.float64) if unity_sum is None else unity_sum + x
    max_sum_peak = float(np.max(np.abs(unity_sum)))
    worst = max(max_stem_peak, max_sum_peak)
    gain = min(1.0, target_peak / worst) if worst > 0 else 1.0
    return {
        "linear": round(gain, 6),
        "db": round(20.0 * math.log10(gain), 3) if gain > 0 else float("-inf"),
        "max_float_stem_peak": round(max_stem_peak, 6),
        "max_unity_sum_peak": round(max_sum_peak, 6),
    }


def write_float_stems(
    stems: dict[str, np.ndarray],
    gain: float,
    sample_rate: int,
    output_dir: Path,
) -> dict[str, Path]:
    """Apply the common gain and write float32 WAV stems."""
    import soundfile as sf

    output_dir.mkdir(parents=True, exist_ok=True)
    paths: dict[str, Path] = {}
    for name in STEM_ORDER:
        path = output_dir / f"{name}.wav"
        sf.write(path, (stems[name] * gain).astype(np.float32), sample_rate,
                 subtype="FLOAT")
        paths[name] = path
    return paths


def encode_aac_stems(
    stem_wavs: dict[str, Path],
    output_dir: Path,
    bitrate: str,
) -> dict[str, Path]:
    """
    Encode float stems to AAC .m4a at the configured bitrate.

    No resample: stems stay at the model rate so the decoded-AAC integrity
    gate compares sample-aligned signals. +faststart for browser streaming.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    encoded: dict[str, Path] = {}
    for name in STEM_ORDER:
        m4a = output_dir / f"{name}.m4a"
        _run([
            "ffmpeg", "-y",
            "-i", str(stem_wavs[name]),
            "-c:a", "aac", "-profile:a", "aac_low", "-b:a", bitrate,
            "-ar", "44100", "-ac", "2",
            "-movflags", "+faststart",
            str(m4a),
        ])
        encoded[name] = m4a
    return encoded


def decode_stem(path: Path, output_wav: Path) -> None:
    """Decode an encoded stem back to float32 PCM (for the decoded-AAC gate)."""
    _run([
        "ffmpeg", "-y",
        "-i", str(path),
        "-c:a", "pcm_f32le",
        str(output_wav),
    ])


def create_multitrack_mp4(
    video_only: Path,
    stem_m4as: dict[str, Path],
    output_path: Path,
) -> None:
    """
    Create multi-track.mp4: video + 6 AAC audio tracks (no original stereo).

    Audio streams are stream-copied from the already-encoded AAC stems so the
    mux honors the bitrate knob and never re-encodes (no second-generation
    AAC loss).
    """
    cmd = [
        "ffmpeg", "-hide_banner", "-y",
        "-i", str(video_only),
    ]
    for name in STEM_ORDER:
        cmd += ["-i", str(stem_m4as[name])]
    cmd += ["-map", "0:v"]
    for i, name in enumerate(STEM_ORDER):
        cmd += [
            "-map", f"{i + 1}:a",
            f"-metadata:s:a:{i}", f"handler_name={STEM_DISPLAY_NAMES[name]}",
        ]
    cmd += [
        "-c:v", "copy",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-disposition:a:0", "none",
        str(output_path),
    ]
    _run(cmd)
