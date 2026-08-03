#!/usr/bin/env python
"""Spike 0.3 -- Demucs gain/null behavior.

Measures whether `--clip-mode rescale` (legacy k25-nextgen flags) alters relative
stem levels and poisons the unity-sum null test, versus float-preserving output.

Dual-mode script:
  * On the Windows host (sys.platform == "win32"): launches the k25-nextgen
    local-server Docker image (demucs 4.0.1 + CUDA) with this spikes/ tree
    mounted at /spike, and re-executes itself inside the container.
  * Inside the container (linux): extracts canonical audio, runs Demucs twice
    (legacy int24/rescale vs float32/clip-none), sums stems at unity, and
    writes metrics to analysis.json.

Input: work/source.mp4 (copied from X:\\GitHub\\k25\\data\\47bae048e13c -- the
shortest completed job, 199.7 s). Canonical WAV extraction mirrors
k25-nextgen-rewrite local_server.processing.extract_audio (pcm_s16le 48 kHz stereo).

Note: demucs 4.0.1's CLI only exposes --clip-mode {rescale,clamp}, but the
library's prevent_clip() supports 'none'. Run B therefore replicates
demucs.separate.main() via the Python API (identical load path, normalization,
and apply_model parameters) and saves with save_audio(clip='none', as_float=True).
"""
import json
import math
import subprocess
import sys
from pathlib import Path

IMAGE = "k25-nextgen-rewrite-local-server:latest"
MODEL = "htdemucs_6s"
SEGMENT = 7
OVERLAP = 0.25
SHIFTS = 0


def sh(cmd, **kw):
    cmd = [str(c) for c in cmd]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


# --------------------------------------------------------------------------- host
def host_main():
    spike = Path(__file__).resolve().parent          # ...\spikes\demucs-gain
    spikes_root = spike.parent
    cache = spike / "work" / "torch-cache"
    cache.mkdir(parents=True, exist_ok=True)
    sh([
        "docker", "run", "--rm", "--gpus", "all",
        "-v", f"{spikes_root}:/spike",
        "-v", f"{cache}:/root/.cache/torch",
        IMAGE,
        "python", "/spike/demucs-gain/run.py",
    ])
    print("done; see", spike / "analysis.json")


# ---------------------------------------------------------------------- container
def dbfs(x):
    return 20.0 * math.log10(max(float(x), 1e-15))


def stats(arr):
    import numpy as np
    rms = float(np.sqrt(np.mean(arr.astype(np.float64) ** 2)))
    peak = float(np.max(np.abs(arr)))
    return {"rms_dbfs": round(dbfs(rms), 2), "peak_dbfs": round(dbfs(peak), 2),
            "rms": rms, "peak": peak}


def run_b_float(canonical: Path, outdir: Path):
    """Replicate demucs.separate.main() but save float32 with clip='none'."""
    import torch
    from demucs.apply import apply_model
    from demucs.audio import save_audio
    from demucs.pretrained import get_model
    from demucs.separate import load_track

    model = get_model(MODEL)
    model.cpu()
    model.eval()
    wav = load_track(canonical, model.audio_channels, model.samplerate)
    ref = wav.mean(0)
    wav -= ref.mean()
    wav /= ref.std()
    sources = apply_model(model, wav[None], device="cuda", shifts=SHIFTS,
                          split=True, overlap=OVERLAP, progress=True,
                          num_workers=0, segment=SEGMENT)[0]
    sources *= ref.std()
    sources += ref.mean()
    dest = outdir / MODEL / canonical.stem
    dest.mkdir(parents=True, exist_ok=True)
    for source, name in zip(sources, model.sources):
        save_audio(source, str(dest / f"{name}.wav"),
                   samplerate=model.samplerate, clip="none", as_float=True)
    print("cuda device used:", torch.cuda.get_device_name(0))
    return list(model.sources), dest


def container_main():
    import numpy as np
    import soundfile as sf

    spike = Path("/spike/demucs-gain")
    work = spike / "work"
    src = work / "source.mp4"
    canonical = work / "canonical.wav"

    # 1. canonical extraction -- mirrors k25-nextgen extract_audio()
    sh(["ffmpeg", "-y", "-i", src, "-vn", "-acodec", "pcm_s16le",
        "-ar", "48000", "-ac", "2", canonical])

    # 2. reference at model rate. Demucs load_track decodes via ffmpeg with
    #    -ar 44100 -f f32le (AudioFile.read); reproduce that exact path so the
    #    null test compares against precisely what the model consumed.
    ref44 = work / "ref44.wav"
    sh(["ffmpeg", "-y", "-i", canonical, "-ar", "44100", "-ac", "2",
        "-c:a", "pcm_f32le", ref44])

    # 3. Run A -- legacy production flags (int24 + per-stem rescale)
    run_a = work / "runA"
    sh([sys.executable, "-m", "demucs", "-n", MODEL,
        "--segment", str(SEGMENT), "--overlap", str(OVERLAP),
        "--shifts", str(SHIFTS), "--clip-mode", "rescale", "--int24",
        "--out", str(run_a), str(canonical)])
    dir_a = run_a / MODEL / canonical.stem

    # 4. Run B -- float-preserving (clip='none', float32)
    stems, dir_b = run_b_float(canonical, work / "runB")

    # 5. Analysis
    ref, sr = sf.read(ref44, dtype="float64")
    assert sr == 44100
    result = {"song": "k25/data/47bae048e13c (shortest, 199.7s)",
              "model": MODEL, "segment": SEGMENT, "overlap": OVERLAP,
              "shifts": SHIFTS, "sample_rate": sr,
              "ref": stats(ref), "stems": {}, "runs": {}}

    sums = {}
    for label, d in (("A_int24_rescale", dir_a), ("B_float32_clipnone", dir_b)):
        total = None
        for name in stems:
            x, s = sf.read(d / f"{name}.wav", dtype="float64")
            assert s == 44100
            st = result["stems"].setdefault(name, {})
            st[label] = stats(x)
            total = x if total is None else total + x
        sums[label] = total

    # per-stem implied gain (A relative to B) -- exposes rescale attenuation
    for name, st in result["stems"].items():
        ra, rb = st["A_int24_rescale"]["rms"], st["B_float32_clipnone"]["rms"]
        st["gain_A_vs_B_db"] = round(20 * math.log10(ra / rb), 3) if rb > 0 else None

    for label, total in sums.items():
        n = min(len(total), len(ref))
        resid = total[:n] - ref[:n]
        r = {"sum": stats(total), "residual": stats(resid),
             "null_depth_db": round(result["ref"]["rms_dbfs"] - dbfs(
                 float(np.sqrt(np.mean(resid ** 2)))), 2),
             "n_samples": n}
        result["runs"][label] = r

    # gain policy: one common gain giving all float stems >= ~0.09 dB headroom
    max_stem_peak = max(st["B_float32_clipnone"]["peak"]
                        for st in result["stems"].values())
    max_sum_peak = float(np.max(np.abs(sums["B_float32_clipnone"])))
    worst = max(max_stem_peak, max_sum_peak)
    g = min(1.0, 0.99 / worst)
    result["gain_policy"] = {
        "max_float_stem_peak": round(max_stem_peak, 6),
        "max_unity_sum_peak": round(max_sum_peak, 6),
        "common_gain": round(g, 6),
        "common_gain_db": round(20 * math.log10(g), 3),
        "note": "single gain applied identically to all 6 stems (and implied mix); "
                "recorded in manifest; preserves relative levels exactly",
    }

    # strip raw linear values from the JSON for readability
    def clean(d):
        for k in ("rms", "peak"):
            d.pop(k, None)
    clean(result["ref"])
    for st in result["stems"].values():
        for lbl in ("A_int24_rescale", "B_float32_clipnone"):
            clean(st[lbl])
    for r in result["runs"].values():
        clean(r["sum"])
        clean(r["residual"])

    out = spike / "analysis.json"
    out.write_text(json.dumps(result, indent=2))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    if sys.platform == "win32":
        host_main()
    else:
        container_main()
