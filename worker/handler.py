"""
Shizzle GPU worker — RunPod serverless handler + local mode.

Owns everything media-heavy (design spec section 3): audio extract,
browser-safe H.264 video re-encode, Demucs separation (float-preserving),
common-gain normalization, both integrity gates, AAC stem encode, optional
multi-track.mp4 mux, and staged uploads with per-object sha256 checksums.

Staging layout (output_prefix = tracks/{track_id}/{generation}/staging/):
  - video.mp4                browser-safe video-only (H.264/yuv420p)
  - stems/{stem}.m4a         6x AAC stems (common gain baked in)
  - manifest.json            v3 staging manifest (see MANIFEST.md)
  - multi-track.mp4          optional (CREATE_MULTITRACK_MP4, default true)

The publisher (VPS) verifies staged sizes/sha256 against the result payload
and writes the immutable manifest last. Progress callbacks are OPTIONAL —
no-ops when no CALLBACK_URL is configured.

Local mode (also the CI path — no S3, no RunPod):
    python3 handler.py --local <input.mp4> <output_dir> [--model htdemucs_6s]
or  LOCAL_MODE=1 LOCAL_INPUT=<in.mp4> LOCAL_OUTPUT=<dir> python3 handler.py
"""

import argparse
import json
import os
import shutil
import sys
import time
from pathlib import Path
from typing import Any

import numpy as np

from audio_processing import (
    STEM_ORDER,
    compute_common_gain,
    create_multitrack_mp4,
    decode_reference,
    decode_stem,
    encode_aac_stems,
    extract_audio,
    extract_video_only,
    get_duration,
    get_video_metadata,
    separate_stems,
    write_float_stems,
)
from callbacks import send_completion, send_error, send_progress
from config import JobConfig, JobMetadata, stem_aac_bitrate, validate_input
from integrity import PROFILE_VERSION, run_gate_a, run_gate_b
from manifest import create_staging_manifest, write_manifest
from s3_ops import create_s3_client, download_source, sha256_file, upload_outputs


def _read_wav(path: Path) -> np.ndarray:
    import soundfile as sf

    data, _sr = sf.read(path, dtype="float64")
    return data


def process_track(
    source_mp4: Path,
    base_dir: Path,
    config: JobConfig,
) -> dict[str, Any]:
    """
    Run the full media pipeline on a local source file.

    Produces the staging file set under base_dir/out and returns a result
    dict: duration, common_gain, integrity (both gates), stage timings, and
    the produced files as (path, relative_key) pairs.
    """
    work_dir = base_dir / "work"
    out_dir = base_dir / "out"
    stems_out = out_dir / "stems"
    for d in (work_dir, out_dir, stems_out):
        d.mkdir(parents=True, exist_ok=True)

    timings: dict[str, float] = {}

    def stage(name: str, started: float) -> None:
        timings[name] = round(time.monotonic() - started, 2)
        print(f"[stage] {name}: {timings[name]}s", flush=True)

    # --- probe + extract -----------------------------------------------------
    t = time.monotonic()
    send_progress(config.job_id, "extracting", 10)
    duration = get_duration(source_mp4)
    print(f"Media duration: {duration:.2f} seconds", flush=True)

    audio_wav = work_dir / "audio.wav"
    extract_audio(source_mp4, audio_wav)
    stage("extract_audio", t)

    t = time.monotonic()
    video_only = out_dir / "video.mp4"
    extract_video_only(source_mp4, video_only)
    video_meta = get_video_metadata(video_only)
    stage("encode_video", t)

    # --- demucs (float-preserving, Python API) -------------------------------
    t = time.monotonic()
    send_progress(config.job_id, "splitting", 30)
    stems, sample_rate = separate_stems(audio_wav, config.model)
    stage("demucs", t)

    # --- common gain + gate (a) ---------------------------------------------
    t = time.monotonic()
    send_progress(config.job_id, "verifying", 60)
    common_gain = compute_common_gain(stems)
    gain = common_gain["linear"]
    print(f"Common gain: {gain} ({common_gain['db']} dB)", flush=True)

    # Reference decoded through the identical ffmpeg path Demucs uses, so
    # resampler differences are excluded from the residual (spike 0.3).
    ref_wav = work_dir / "reference.wav"
    decode_reference(audio_wav, ref_wav, sample_rate)
    reference = _read_wav(ref_wav)

    stem_sum = None
    for name in STEM_ORDER:
        x = stems[name].astype(np.float64)
        stem_sum = x if stem_sum is None else stem_sum + x
    gate_a = run_gate_a(reference, stem_sum, gain)
    print(f"Gate (a) PCM reconstruction: null {gate_a['null_depth_db']} dB, "
          f"peak {gate_a['peak_residual_dbfs']} dBFS -> "
          f"{'PASS' if gate_a['passed'] else 'FAIL'}", flush=True)
    stage("gate_a", t)

    # --- write gained float stems + AAC encode -------------------------------
    t = time.monotonic()
    send_progress(config.job_id, "encoding", 70)
    float_dir = work_dir / "stems_f32"
    float_stems = write_float_stems(stems, gain, sample_rate, float_dir)
    stem_m4as = encode_aac_stems(float_stems, stems_out, config.aac_bitrate)
    stage("encode_stems", t)

    # --- gate (b): decoded-AAC delivery --------------------------------------
    t = time.monotonic()
    float_remix = stem_sum * gain
    decoded_sum = None
    for name in STEM_ORDER:
        dec_wav = work_dir / f"decoded_{name}.wav"
        decode_stem(stem_m4as[name], dec_wav)
        d = _read_wav(dec_wav)
        dec_wav.unlink()
        if decoded_sum is None:
            decoded_sum = d
        else:
            n = min(len(decoded_sum), len(d))
            decoded_sum = decoded_sum[:n] + d[:n]
    gate_b = run_gate_b(float_remix, decoded_sum, gain)
    print(f"Gate (b) decoded delivery: null {gate_b['null_depth_db']} dB, "
          f"peak {gate_b['peak_residual_dbfs']} dBFS, "
          f"offset {gate_b['offset']} -> "
          f"{'PASS' if gate_b['passed'] else 'FAIL'}", flush=True)
    stage("gate_b", t)

    # --- optional multi-track mux -------------------------------------------
    multitrack_path = None
    if config.create_multitrack_mp4:
        t = time.monotonic()
        multitrack_path = out_dir / "multi-track.mp4"
        create_multitrack_mp4(video_only, stem_m4as, multitrack_path)
        stage("multitrack_mux", t)

    # --- manifest ------------------------------------------------------------
    integrity = {
        "profile_version": PROFILE_VERSION,
        "gate_a": gate_a,
        "gate_b": gate_b,
    }
    processing = {
        "model": config.model,
        "segment": 7,
        "overlap": 0.25,
        "shifts": 0,
        "stem_codec": "aac",
        "stem_bitrate": config.aac_bitrate,
        "float_preserved": True,
    }
    manifest = create_staging_manifest(
        config.metadata, duration, sample_rate, common_gain, integrity,
        processing, video_meta=video_meta,
        multitrack=multitrack_path is not None,
    )
    manifest_path = out_dir / "manifest.json"
    write_manifest(manifest, manifest_path)

    files: list[tuple[Path, str]] = [
        (video_only, "video.mp4"),
        (manifest_path, "manifest.json"),
    ]
    if multitrack_path is not None:
        files.append((multitrack_path, "multi-track.mp4"))
    for name in STEM_ORDER:
        files.append((stem_m4as[name], f"stems/{name}.m4a"))

    return {
        "duration": duration,
        "sample_rate": sample_rate,
        "common_gain": common_gain,
        "integrity": integrity,
        "timings": timings,
        "files": files,
    }


def handler(job: dict) -> dict:
    """
    Main handler for RunPod serverless processing (S3 in / S3 staging out).

    Input schema: see config.validate_input. Returns status, staged uploads
    with per-object sha256 checksums, integrity results, and stage timings.
    """
    inp = job.get("input", {})
    config = validate_input(inp)

    s3 = create_s3_client()

    base = Path("/tmp/work")
    shutil.rmtree(base, ignore_errors=True)
    in_dir = base / "in"
    in_dir.mkdir(parents=True, exist_ok=True)

    t_total = time.monotonic()
    t = time.monotonic()
    source_mp4 = in_dir / "source.mp4"
    download_source(s3, config.bucket, config.input_key, source_mp4)
    download_secs = round(time.monotonic() - t, 2)

    result_core = process_track(source_mp4, base, config)

    t = time.monotonic()
    send_progress(config.job_id, "uploading", 90)
    uploads = upload_outputs(
        s3, config.bucket, config.output_prefix, result_core["files"]
    )
    upload_secs = round(time.monotonic() - t, 2)

    timings = {"download": download_secs, **result_core["timings"],
               "upload": upload_secs,
               "total": round(time.monotonic() - t_total, 2)}

    send_progress(config.job_id, "complete", 100)

    result = {
        "status": "ok",
        "job_id": config.job_id,
        "track_id": config.track_id,
        "generation": config.generation,
        "bucket": config.bucket,
        "output_prefix": config.output_prefix,
        "duration": result_core["duration"],
        "common_gain": result_core["common_gain"],
        "integrity": result_core["integrity"],
        "uploads": uploads,
        "uploaded_count": len(uploads),
        "multitrack_mp4_created": config.create_multitrack_mp4,
        "timings": timings,
    }

    send_completion(result)
    return result


def handler_wrapper(job: dict) -> dict:
    """Wrapper to catch exceptions and send failure callbacks (if configured)."""
    try:
        return handler(job)
    except Exception as e:
        job_id = job.get("input", {}).get("job_id", "unknown")
        send_error(job_id, str(e))
        raise


def run_local(input_path: str, output_dir: str, model: str = "htdemucs_6s") -> dict:
    """
    Local file-in/file-out mode: full pipeline, no S3, no RunPod.

    This is the smoke-test and CI path. Produces the staging file set under
    <output_dir>/out plus result.json with checksums, integrity, timings.
    """
    source = Path(input_path)
    if not source.exists():
        raise FileNotFoundError(f"Input file not found: {source}")
    base = Path(output_dir)
    base.mkdir(parents=True, exist_ok=True)

    config = JobConfig(
        job_id="local",
        bucket="local",
        input_key="local",
        output_prefix="local/",
        model=model,
        metadata=JobMetadata(title=source.stem),
        create_multitrack_mp4=os.getenv("CREATE_MULTITRACK_MP4", "true").lower()
        in ("1", "true", "yes", "on"),
        aac_bitrate=stem_aac_bitrate(),
    )

    t_total = time.monotonic()
    core = process_track(source, base, config)
    core["timings"]["total"] = round(time.monotonic() - t_total, 2)

    files = [
        {
            "file": rel,
            "sha256": sha256_file(path),
            "size_bytes": path.stat().st_size,
        }
        for path, rel in core["files"]
    ]
    result = {
        "status": "ok",
        "mode": "local",
        "duration": core["duration"],
        "sample_rate": core["sample_rate"],
        "common_gain": core["common_gain"],
        "integrity": core["integrity"],
        "files": files,
        "timings": core["timings"],
    }
    result_path = base / "result.json"
    result_path.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2), flush=True)
    print(f"Local run complete: {base / 'out'} (result: {result_path})", flush=True)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Shizzle GPU worker")
    parser.add_argument("--local", nargs=2, metavar=("INPUT", "OUTPUT_DIR"),
                        help="run locally on a file, no S3/RunPod")
    parser.add_argument("--model", default="htdemucs_6s")
    args = parser.parse_args()

    local_env = os.getenv("LOCAL_MODE", "").lower() in ("1", "true", "yes", "on")
    if args.local:
        run_local(args.local[0], args.local[1], args.model)
    elif local_env:
        inp = os.getenv("LOCAL_INPUT")
        out = os.getenv("LOCAL_OUTPUT")
        if not inp or not out:
            print("LOCAL_MODE=1 requires LOCAL_INPUT and LOCAL_OUTPUT", file=sys.stderr)
            sys.exit(2)
        run_local(inp, out, args.model)
    else:
        import runpod

        runpod.serverless.start({"handler": handler_wrapper})


if __name__ == "__main__":
    main()
