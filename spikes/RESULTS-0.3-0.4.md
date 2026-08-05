# Spike results: 0.3 Demucs gain/null behavior, 0.4 AAC renditions

> Troubleshooting experiment for separation gain, reconstruction, clipping, and
> delivery-codec questions. It is not current completion work.

Date: 2026-08-02. Executed on the local RTX 4070 (Laptop GPU) via the
`k25-nextgen-rewrite-local-server:latest` Docker image (demucs 4.0.1, torch CUDA,
ffmpeg 4.4.2). GPU use confirmed in demucs/torch logs both runs.

**Song:** `X:\GitHub\k25\data\47bae048e13c\source.mp4` — shortest completed k25
job (3:19.7; other jobs 3:40–9:17). Reference WAV extracted exactly as
k25-nextgen `processing.extract_audio` (pcm_s16le, 48 kHz, stereo). Demucs
consumes it via its own ffmpeg decode at 44 100 Hz; the null-test reference was
produced through that identical ffmpeg path (`-ar 44100 -c:a pcm_f32le`), so
resampler differences are excluded from the residual.

Scripts: `spikes/demucs-gain/run.py`, `spikes/aac-abx/make_renditions.py`
(both dual-mode: run on Windows, they re-execute themselves inside the Docker
image). Raw metrics: `spikes/demucs-gain/analysis.json`,
`spikes/aac-abx/analysis-aac.json`.

---

## Spike 0.3 — rescale vs float (htdemucs_6s, segment 7, overlap 0.25, shifts 0)

- Run A (legacy production flags): `--clip-mode rescale --int24`
- Run B (float-preserving): Python-API replication of `demucs.separate.main`
  with `save_audio(clip='none', as_float=True)`. **Deviation:** demucs 4.0.1's
  CLI only exposes `--clip-mode {rescale,clamp}`; the library's `prevent_clip`
  supports `'none'`, so run B replicates the CLI load path, normalization and
  `apply_model` parameters exactly and only changes the save step.

### Per-stem levels (dBFS)

| Stem | A rms | A peak | B rms | B peak | implied A-vs-B gain |
|---|---|---|---|---|---|
| drums  | -19.98 | -0.09 | -18.75 | **+1.15** | **-1.236 dB** |
| bass   | -18.36 | -7.34 | -18.36 | -7.34 | 0 |
| other  | -53.14 | -20.22 | -53.14 | -20.22 | 0 |
| vocals | -20.88 | -0.86 | -20.88 | -0.86 | 0 |
| guitar | -15.68 | -0.09 | -15.56 | **+0.04** | **-0.128 dB** |
| piano  | -37.03 | -9.32 | -37.03 | -9.32 | 0 |

### Unity-sum null test (vs 44.1 kHz float reference, RMS -10.86 dBFS, 8 805 377 samples)

| Run | sum rms | residual RMS | residual peak | null depth |
|---|---|---|---|---|
| A int24+rescale | -11.30 | -31.07 dBFS | -10.75 dBFS | 20.21 dB |
| B float32+none  | -11.01 | **-33.43 dBFS** | -10.95 dBFS | **22.57 dB** |

### Verdict: review item 5 confirmed

`--clip-mode rescale` attenuated **drums by 1.24 dB and guitar by 0.13 dB**
relative to the other four stems on this one ordinary track (any float stem
peaking above ~0.99 gets its own divisor). That is an audible mix-balance shift
and it degraded the null by 2.4 dB. Rescale is per-stem, silent, and
track-dependent — it poisons both the unity-sum gate and the actual karaoke mix
balance. The legacy flags must not be carried into Shizzle.

**Recommended gain policy** (matches design §5):

1. Demucs output saved float32 with `clip='none'` (Python API; CLI cannot do it).
2. One common gain applied to all 6 stems identically, chosen from
   `max(all stem peaks, unity-sum peak)` with ~0.09 dB margin to 0.99 FS:
   for this track `g = 0.854274` (**-1.37 dB**). Computed per track, recorded in
   the manifest, applied to every stem — relative levels preserved exactly.
3. Note the ceiling: even a perfect pipeline nulls at only ~22.6 dB —
   htdemucs_6s stems do not sum exactly to the input (model-inherent). The
   gates are pipeline-fault detectors, not quality scores.

### Proposed thresholds (profile v1 — from one track; re-validate on ~5 tracks in phase 1 before freezing)

- **Gate (a) — PCM reconstruction (float stems × common gain vs gained input):**
  null depth >= 15 dB (measured 22.6, ~7 dB margin) AND residual peak
  <= -6 dBFS (measured -10.95) AND sample counts equal AND offset = 0.
- **Gate (b) — decoded-delivery (decoded AAC stems summed vs float remix):**
  null depth >= 12 dB (measured 19.2 at 256k) AND residual peak <= 0 dBFS
  (measured -3.3) AND best-fit offset = 0 samples (measured 0 — ffmpeg m4a
  gapless metadata holds) AND decoded length within codec-padding tolerance
  (measured: 1 sample short).
- Store per track: sample count, offset, RMS residual, peak residual,
  threshold-profile version (per design §5).

---

## Spike 0.4 — AAC renditions for blinded listening

From the run-B float stems × common gain (-1.37 dB): each stem encoded
**AAC 256k**, **AAC 320k** (ffmpeg native aac), and **ALAC** (s32p); each
rendition's stems decoded and summed at unity — the exact player operation —
into one stereo remix. Remix/blind files are ALAC (lossless), so only
stem-codec damage is audible.

### Objective (decoded rendition remix vs float reference remix)

| Rendition | remix peak | offset | residual RMS | residual peak | null depth |
|---|---|---|---|---|---|
| AAC 256k | **+0.96 dBFS** | 0 | -31.54 dBFS | -3.32 dBFS | 19.16 dB |
| AAC 320k | **+0.82 dBFS** | 0 | -31.67 dBFS | -2.92 dBFS | 19.29 dB |
| ALAC     | -0.09 dBFS | 0 | **-129.0 dBFS** | -123.3 dBFS | 116.6 dB |

- ALAC round-trip is bit-transparent for practical purposes (-129 dBFS =
  24-bit-floor territory) — sanity check on the whole measurement chain.
- 256k vs 320k are objectively near-identical (0.13 dB apart); the difference,
  if any, will only show in the blind listen.
- **Headroom finding:** decoded AAC overshoots its input — the summed remix
  peaked ~+1 dBFS despite the -1.37 dB pre-gain. The player's master bus needs
  either ~2 dB additional headroom in the common gain or the planned master
  limiter (design phase 5); otherwise unity playback of AAC stems will clip.

### Rendition inventory (`spikes/aac-abx/`)

- `out/REF.m4a` — labeled reference (float-stem remix, ALAC)
- `out/A.m4a`, `out/B.m4a`, `out/C.m4a` — the three renditions, randomized
- `out/answer_key.json` — mapping (not read by the agent; do not open pre-listen)
- `out/private/stems_{aac256,aac320,alac}/*.m4a` — 18 per-stem encoded files
- `LISTEN.md` — rig instructions + suggested decision rule
- All four blind files carry one common -3 dB trim (identical across files —
  level-match preserved) so the AAC overshoot cannot clip the ALAC blind encode
  and contaminate the ABX.
- Known side-channel: file sizes/durations differ slightly between blind files;
  judge by ear only.

---

## Deviations from brief

- `--clip-mode none` / `--float32` CLI combo impossible in demucs 4.0.1 (CLI
  choices are rescale|clamp only); run B uses the documented Python-API
  replication instead — same model call, only the save differs.
- The k25-nextgen Docker image had no baked model cache; htdemucs_6s weights
  download to a mounted cache (`spikes/demucs-gain/work/torch-cache/`).
- An earlier rendition build printed the blind mapping into a console log; the
  blind files were re-randomized and re-encoded with mapping output suppressed.

## PENDING

**PENDING: Mike's blind listen** — play `spikes/aac-abx/out/` on the real rig
per `LISTEN.md`, record distinguishability + ranking, then unblind with
`answer_key.json` and replace this line with the verdict.
