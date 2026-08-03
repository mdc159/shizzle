"""
Runpod Serverless Handler for Karaoke Demucs Processing

Input: MP4 video with audio
Output (staging layout per s3-naming-spec.md):
  - video.mp4 (video-only for web mixer)
  - stems/vocals.wav, stems/drums.wav, etc. (6x WAV stems in stems/ subfolder)
  - manifest.json (staging manifest with durations, hashes)
  - karaoke_six_stem.mp4 (optional canonical multi-audio)

This handler orchestrates the processing pipeline using modular components:
  - config.py: Input validation and configuration
  - s3_ops.py: S3 download/upload with verification
  - audio_processing.py: FFmpeg extraction, Demucs separation
  - manifest.py: Manifest generation
  - callbacks.py: Progress & completion callbacks with authentication
"""

import shutil
from pathlib import Path

import runpod

from audio_processing import (
    STEM_ORDER,
    create_multi_audio_mp4,
    extract_audio,
    extract_video_only,
    get_duration,
    run_demucs,
)
from callbacks import send_completion, send_error, send_progress
from config import validate_input
from manifest import create_staging_manifest, write_manifest
from s3_ops import create_s3_client, download_source, upload_outputs


def handler(job: dict) -> dict:
    """
    Main handler for Runpod serverless processing.

    Expected input:
    {
      "input": {
        "job_id": "recXXXX",
        "bucket": "karaoke-pimpshizzle",
        "input_key": "karaoke/in/recXXXX/source.mp4",
        "output_prefix": "karaoke/out/recXXXX/",
        "model": "htdemucs_6s",
        "create_canonical_mp4": true,  // Optional, defaults to true
        "metadata": {
          "title": "Song Title",
          "artist": "Artist Name",
          "sourceUrl": "https://youtube.com/...",
          "videoId": "dQw4w9WgXcQ"
        }
      }
    }

    Returns:
        Dict with status, uploads, and job metadata
    """
    # 1. Validate input and create typed config
    inp = job.get("input", {})
    config = validate_input(inp)

    s3 = create_s3_client()

    # 2. Setup workspace
    base = Path("/tmp/work")
    in_dir = base / "in"
    out_dir = base / "out"
    encoded_dir = base / "encoded"

    shutil.rmtree(base, ignore_errors=True)
    for d in [in_dir, out_dir, encoded_dir]:
        d.mkdir(parents=True, exist_ok=True)

    # 3. Download source from S3
    source_mp4 = in_dir / "source.mp4"
    download_source(s3, config.bucket, config.input_key, source_mp4)

    # 4. Extract audio and video
    send_progress(config.job_id, "extracting", 10)

    duration = get_duration(source_mp4)
    print(f"Video duration: {duration:.2f} seconds", flush=True)

    audio_wav = in_dir / "audio.wav"
    extract_audio(source_mp4, audio_wav)

    video_only = encoded_dir / "video.mp4"
    extract_video_only(source_mp4, video_only)

    # 5. Run Demucs stem separation
    send_progress(config.job_id, "splitting", 30)
    stem_wavs = run_demucs(audio_wav, out_dir, config.model)

    # 6. Create canonical multi-audio MP4 (optional)
    karaoke_mp4 = None
    if config.create_canonical_mp4:
        karaoke_mp4 = encoded_dir / "karaoke_six_stem.mp4"
        create_multi_audio_mp4(video_only, stem_wavs, karaoke_mp4)

    # 7. Generate staging manifest
    manifest = create_staging_manifest(config.metadata, duration, stem_wavs)
    manifest_path = encoded_dir / "manifest.json"
    write_manifest(manifest, manifest_path)

    # 8. Upload all outputs to S3
    send_progress(config.job_id, "encoding", 80)

    # Build list of files to upload: (local_path, relative_key)
    files_to_upload: list[tuple[Path, str]] = [
        (video_only, "video.mp4"),
        (manifest_path, "manifest.json"),
    ]

    # Add canonical MP4 if created
    if karaoke_mp4 and karaoke_mp4.exists():
        files_to_upload.append((karaoke_mp4, "karaoke_six_stem.mp4"))

    # Add stem WAV files
    for stem in STEM_ORDER:
        if stem in stem_wavs:
            files_to_upload.append((stem_wavs[stem], f"stems/{stem}.wav"))

    uploads = upload_outputs(s3, config.bucket, config.output_prefix, files_to_upload)

    # 9. Send completion callback
    send_progress(config.job_id, "complete", 100)

    result = {
        "status": "ok",
        "job_id": config.job_id,
        "bucket": config.bucket,
        "output_prefix": config.output_prefix,
        "duration": duration,
        "uploads": uploads,
        "uploaded_count": len(uploads),
        "canonical_mp4_created": config.create_canonical_mp4,
    }

    send_completion(result)

    return result


def handler_wrapper(job: dict) -> dict:
    """Wrapper to catch exceptions and send failure callbacks."""
    try:
        return handler(job)
    except Exception as e:
        job_id = job.get("input", {}).get("job_id", "unknown")
        send_error(job_id, str(e))
        raise


runpod.serverless.start({"handler": handler_wrapper})
