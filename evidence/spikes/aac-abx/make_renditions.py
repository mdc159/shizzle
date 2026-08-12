#!/usr/bin/env python
"""Spike 0.4 -- AAC renditions for blinded listening.

Takes the float32 Demucs stems from spike 0.3 run B, applies the one common
gain from the spike 0.3 gain policy, encodes each stem three ways
(AAC 256k, AAC 320k, ALAC), then remixes each rendition's *decoded* stems at
unity into a single stereo remix -- simulating the delivery pipeline where
stems are stored lossy and summed at playback. The remix files themselves are
encoded ALAC (lossless) so only stem-codec artifacts are audible.

Outputs (evidence/spikes/aac-abx/out/):
  REF.m4a           remix of the raw float stems (labeled reference, ALAC)
  A.m4a B.m4a C.m4a the three renditions under randomized blind names (ALAC)
  answer_key.json   blind-name -> codec mapping (do not open before listening)
  private/          per-stem encoded files, named by codec (do not open)

Objective checks (decoded rendition remix vs float reference remix, RMS/peak
residual with small lag search) are written to analysis-aac.json, keyed by
codec name only -- no blind letters -- so reading it does not unblind the test.

Dual-mode like spike 0.3's run.py: on Windows it re-executes itself inside the
k25-nextgen local-server image (ffmpeg 4.4.2 + numpy + soundfile); the AAC
encoder is ffmpeg's native aac.
"""
import json
import math
import random
import subprocess
import sys
from pathlib import Path

IMAGE = "k25-nextgen-rewrite-local-server:latest"
STEMS = ["drums", "bass", "other", "vocals", "guitar", "piano"]
# Applied identically to REF and all renditions before the lossless blind
# encode: decoded AAC stems overshoot (measured remix peak +0.96 dBFS at 256k),
# and ALAC s32p clips above 0 dBFS. A common trim keeps the files level-matched
# while preventing clipping from contaminating the ABX.
BLIND_TRIM_DB = -3.0
RENDITIONS = {
    "aac256": ["-c:a", "aac", "-b:a", "256k"],
    "aac320": ["-c:a", "aac", "-b:a", "320k"],
    "alac":   ["-c:a", "alac", "-sample_fmt", "s32p"],
}


def sh(cmd, **kw):
    cmd = [str(c) for c in cmd]
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True, **kw)


def host_main():
    spike = Path(__file__).resolve().parent          # ...\evidence\spikes\aac-abx
    spikes_root = spike.parent
    sh([
        "docker", "run", "--rm",
        "-v", f"{spikes_root}:/spike",
        IMAGE,
        "python", "/spike/aac-abx/make_renditions.py",
    ])
    print("done; see", spike / "out", "and", spike / "analysis-aac.json")


def dbfs(x):
    return 20.0 * math.log10(max(float(x), 1e-15))


def container_main():
    import numpy as np
    import soundfile as sf

    spike = Path("/spike/aac-abx")
    out = spike / "out"
    private = out / "private"
    tmp = spike / "tmp"
    for d in (out, private, tmp):
        d.mkdir(parents=True, exist_ok=True)

    stem_dir = Path("/spike/demucs-gain/work/runB/htdemucs_6s/canonical")
    gain = json.loads(
        Path("/spike/demucs-gain/analysis.json").read_text())["gain_policy"]
    g = gain["common_gain"]
    print(f"common gain: {g} ({gain['common_gain_db']} dB)")

    # 1. gained float stems (the canonical pipeline intermediates)
    sr = None
    gained = {}
    for name in STEMS:
        x, s = sf.read(stem_dir / f"{name}.wav", dtype="float64")
        sr = sr or s
        assert s == sr
        gained[name] = x * g
        sf.write(tmp / f"{name}.wav", (x * g).astype(np.float32), sr,
                 subtype="FLOAT")

    # 2. reference remix: unity sum of gained float stems
    ref_mix = np.sum([gained[n] for n in STEMS], axis=0)
    print("ref remix peak dBFS:", round(dbfs(np.max(np.abs(ref_mix))), 2))
    trim = 10.0 ** (BLIND_TRIM_DB / 20.0)
    ref_wav = tmp / "ref_mix.wav"
    sf.write(ref_wav, (ref_mix * trim).astype(np.float32), sr, subtype="FLOAT")
    sh(["ffmpeg", "-y", "-i", ref_wav, "-c:a", "alac", "-sample_fmt", "s32p",
        out / "REF.m4a"])

    # 3. per-rendition: encode stems, decode, unity-sum, save lossless remix
    results = {}
    mixes = {}
    for rend, codec_args in RENDITIONS.items():
        rdir = private / f"stems_{rend}"
        rdir.mkdir(parents=True, exist_ok=True)
        total = None
        for name in STEMS:
            enc = rdir / f"{name}.m4a"
            sh(["ffmpeg", "-y", "-i", tmp / f"{name}.wav", *codec_args, enc])
            dec = tmp / f"dec_{rend}_{name}.wav"
            sh(["ffmpeg", "-y", "-i", enc, "-c:a", "pcm_f32le", dec])
            x, s = sf.read(dec, dtype="float64")
            assert s == sr
            total = x if total is None else total[:len(x)] + x[:len(total)]
        mixes[rend] = total
        mix_wav = tmp / f"mix_{rend}.wav"
        sf.write(mix_wav, (total * trim).astype(np.float32), sr, subtype="FLOAT")
        results[rend] = {"remix_peak_dbfs": round(dbfs(np.max(np.abs(total))), 2)}

    # 4. objective residuals vs float reference remix (with small lag search,
    #    since AAC encode/decode can shift alignment despite gapless metadata)
    for rend, mix in mixes.items():
        best = None
        for lag in range(-4096, 4097, 32):
            if lag >= 0:
                a, b = ref_mix[lag:], mix
            else:
                a, b = ref_mix, mix[-lag:]
            n = min(len(a), len(b), sr * 30)          # 30 s window for speed
            resid = a[:n] - b[:n]
            rms = float(np.sqrt(np.mean(resid ** 2)))
            if best is None or rms < best[1]:
                best = (lag, rms)
        lag = best[0]
        # refine +-32 around best
        for l2 in range(lag - 32, lag + 33):
            if l2 >= 0:
                a, b = ref_mix[l2:], mix
            else:
                a, b = ref_mix, mix[-l2:]
            n = min(len(a), len(b), sr * 30)
            resid = a[:n] - b[:n]
            rms = float(np.sqrt(np.mean(resid ** 2)))
            if rms < best[1]:
                best = (l2, rms)
        lag = best[0]
        if lag >= 0:
            a, b = ref_mix[lag:], mix
        else:
            a, b = ref_mix, mix[-lag:]
        n = min(len(a), len(b))
        resid = a[:n] - b[:n]
        rms = float(np.sqrt(np.mean(resid ** 2)))
        ref_rms = float(np.sqrt(np.mean(ref_mix.astype(np.float64) ** 2)))
        results[rend].update({
            "lag_samples": lag,
            "residual_rms_dbfs": round(dbfs(rms), 2),
            "residual_peak_dbfs": round(dbfs(np.max(np.abs(resid))), 2),
            "null_depth_db": round(dbfs(ref_rms) - dbfs(rms), 2),
            "n_samples": n,
        })

    # 5. blind names
    letters = ["A", "B", "C"]
    rends = list(RENDITIONS)
    random.shuffle(rends)
    key = {}
    print("encoding blind files (mapping suppressed; see out/answer_key.json)")
    for letter, rend in zip(letters, rends):
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(tmp / f"mix_{rend}.wav"),
             "-c:a", "alac", "-sample_fmt", "s32p", str(out / f"{letter}.m4a")],
            check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        key[letter] = rend
    (out / "answer_key.json").write_text(json.dumps(key, indent=2))

    meta = {
        "source": "spike 0.3 run B float stems, common gain "
                  f"{g} ({gain['common_gain_db']} dB)",
        "sample_rate": sr,
        "encoder": "ffmpeg 4.4.2 native aac / alac; remix files ALAC s32p",
        "blind_trim_db": BLIND_TRIM_DB,
        "renditions": results,
    }
    Path(spike / "analysis-aac.json").write_text(json.dumps(meta, indent=2))
    print(json.dumps(meta, indent=2))
    print("blind files written:", sorted(p.name for p in out.glob("*.m4a")))
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    if sys.platform == "win32":
        host_main()
    else:
        container_main()
