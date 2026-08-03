#!/usr/bin/env python3
"""
Local test script for Demucs stem separation.
Run inside the Docker container for testing without Runpod/S3.

Usage:
  docker run --gpus all -v $(pwd)/test:/work demucs-worker python3 local_test.py /work/input.mp4 /work/output
"""
import json
import subprocess
import sys
from pathlib import Path


def _run(cmd: list[str], capture=False):
    """Run command with logging."""
    print("RUN:", " ".join(cmd), flush=True)
    if capture:
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return result.stdout
    subprocess.run(cmd, check=True)
    return None


def _get_duration(video_path: Path) -> float:
    """Get video duration in seconds using ffprobe."""
    cmd = [
        "ffprobe", "-v", "quiet",
        "-show_entries", "format=duration",
        "-of", "json",
        str(video_path)
    ]
    output = _run(cmd, capture=True)
    data = json.loads(output)
    return float(data.get("format", {}).get("duration", 0))


def process_local(input_path: str, output_dir: str, model: str = "htdemucs_6s"):
    """
    Process a local video file through Demucs.

    Args:
        input_path: Path to input video (MP4)
        output_dir: Directory for output files
        model: Demucs model (htdemucs_6s for 6-stem)
    """
    source_mp4 = Path(input_path)
    out_base = Path(output_dir)

    if not source_mp4.exists():
        raise FileNotFoundError(f"Input file not found: {source_mp4}")

    # Clean and create output dirs
    work_dir = out_base / "work"
    encoded_dir = out_base / "encoded"
    demucs_dir = work_dir / "demucs"

    for d in [work_dir, encoded_dir, demucs_dir]:
        d.mkdir(parents=True, exist_ok=True)

    print(f"\n{'='*60}")
    print(f"Processing: {source_mp4.name}")
    print(f"Model: {model}")
    print(f"Output: {out_base}")
    print(f"{'='*60}\n")

    # 1. Get video duration
    duration = _get_duration(source_mp4)
    print(f"Video duration: {duration:.2f} seconds\n")

    # 2. Extract audio to WAV for Demucs
    audio_wav = work_dir / "audio.wav"
    print("Step 1: Extracting audio...")
    _run([
        "ffmpeg", "-y",
        "-i", str(source_mp4),
        "-vn",
        "-acodec", "pcm_s16le",
        "-ar", "44100",
        "-ac", "2",
        str(audio_wav)
    ])

    # 3. Extract video-only track
    video_only = encoded_dir / "video.mp4"
    print("\nStep 2: Extracting video track...")
    _run([
        "ffmpeg", "-y",
        "-i", str(source_mp4),
        "-an",
        "-c:v", "copy",
        str(video_only)
    ])

    # 4. Run Demucs separation
    # Using exact parameters from working Demucs-GUI config
    print(f"\nStep 3: Running Demucs ({model})...")
    _run([
        "python3", "-m", "demucs",
        "-n", model,
        "--segment", "7",  # Max for htdemucs_6s is 7.8, use 7
        "--overlap", "0.25",
        "--shifts", "0",
        "--clip-mode", "rescale",
        "--int24",  # 24-bit for better quality intermediate
        "--out", str(demucs_dir),
        str(audio_wav)
    ])

    # Find demucs output
    track_dir = None
    for p in demucs_dir.rglob("audio"):
        if p.is_dir() and list(p.glob("*.wav")):
            track_dir = p
            break

    if not track_dir:
        raise RuntimeError("Demucs output not found")

    print(f"Demucs output: {track_dir}")

    # 5. Define stems
    stem_order = ["vocals", "drums", "bass", "guitar", "piano", "other"]
    stem_display_names = {
        "vocals": "Vocals",
        "drums": "Drums",
        "bass": "Bass",
        "guitar": "Guitar",
        "piano": "Piano",
        "other": "Shizzle"  # The pimpshizzle touch!
    }

    # Verify all stems
    stem_wavs = {}
    print("\nStep 4: Verifying stems...")
    for stem in stem_order:
        wav_path = track_dir / f"{stem}.wav"
        if wav_path.exists():
            size_mb = wav_path.stat().st_size / (1024**2)
            print(f"  ✓ {stem}.wav ({size_mb:.1f} MB)")
            stem_wavs[stem] = wav_path
        else:
            print(f"  ✗ {stem}.wav MISSING")
            raise RuntimeError(f"Missing stem: {stem}.wav")

    # 6. Encode stems to AAC
    print("\nStep 5: Encoding stems to AAC...")
    stem_m4as = {}
    for stem in stem_order:
        wav_path = stem_wavs[stem]
        m4a_path = encoded_dir / f"{stem}.m4a"
        _run([
            "ffmpeg", "-y",
            "-i", str(wav_path),
            "-c:a", "aac",
            "-b:a", "192k",
            str(m4a_path)
        ])
        stem_m4as[stem] = m4a_path

    # 7. Create multi-audio MP4
    print("\nStep 6: Creating karaoke_six_stem.mp4...")
    karaoke_mp4 = encoded_dir / "karaoke_six_stem.mp4"
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
        "-map", "1:a", "-metadata:s:a:0", f"handler_name={stem_display_names['vocals']}",
        "-map", "2:a", "-metadata:s:a:1", f"handler_name={stem_display_names['drums']}",
        "-map", "3:a", "-metadata:s:a:2", f"handler_name={stem_display_names['bass']}",
        "-map", "4:a", "-metadata:s:a:3", f"handler_name={stem_display_names['guitar']}",
        "-map", "5:a", "-metadata:s:a:4", f"handler_name={stem_display_names['piano']}",
        "-map", "6:a", "-metadata:s:a:5", f"handler_name={stem_display_names['other']}",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "256k",
        "-movflags", "+faststart",
        "-disposition:a:0", "none",
        str(karaoke_mp4)
    ]
    _run(ffmpeg_cmd)

    # 8. Generate stems.json
    print("\nStep 7: Generating stems.json...")
    manifest = {
        "title": source_mp4.stem,
        "artist": "Unknown",
        "duration": duration,
        "video": "video.mp4",
        "sourceUrl": "",
        "videoId": "",
        "stems": [
            {"id": stem, "name": stem, "file": f"{stem}.m4a", "default_gain": 1.0}
            for stem in stem_order
        ]
    }
    manifest_path = encoded_dir / "stems.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    # 9. Summary
    print(f"\n{'='*60}")
    print("OUTPUT FILES:")
    print(f"{'='*60}")
    for f in sorted(encoded_dir.iterdir()):
        size_mb = f.stat().st_size / (1024**2)
        print(f"  {f.name:<25} {size_mb:>8.2f} MB")

    # Verify multi-track
    print(f"\n{'='*60}")
    print("VERIFYING MULTI-TRACK MP4:")
    print(f"{'='*60}")
    _run([
        "ffprobe", "-v", "quiet",
        "-show_entries", "stream=index,codec_type,codec_name",
        "-of", "csv=p=0",
        str(karaoke_mp4)
    ])

    print(f"\n✅ Processing complete! Output in: {encoded_dir}")
    return str(encoded_dir)


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Usage: python3 local_test.py <input.mp4> <output_dir>")
        print("Example: python3 local_test.py /work/test.mp4 /work/output")
        sys.exit(1)

    input_file = sys.argv[1]
    output_dir = sys.argv[2]
    model = sys.argv[3] if len(sys.argv) > 3 else "htdemucs_6s"

    process_local(input_file, output_dir, model)
