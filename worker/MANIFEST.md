# Worker staging manifest schema (v3)

This document describes the cloud GPU worker's staging output. Fresh cloud
separation is active work; once six clean lossless stems exist, the finished
`shizzle-browser-v1` delivery pipeline takes over.

Written by the worker to `{output_prefix}manifest.json` (staging). The
publisher verifies staged objects, then copies the manifest into the immutable
generation prefix **last** — its presence marks the generation complete.

Compatible with the server/UI v3 contract (`version: 3`, `default_gain_db` in
dB) and extends it with worker provenance and integrity results. Earlier v2
`stems.json` files used `default_gain` as a linear value and exposed a latent
zero-is-silence bug; that field name must never return.

## Top-level fields

| Field | Type | Meaning |
|---|---|---|
| `version` | int | Manifest schema version. This document describes `3`. |
| `title`, `artist` | string | From job metadata (defaults `"Unknown"`). |
| `duration` | float | Media duration in seconds (ffprobe on the source). |
| `video` | string | Relative key of the browser-safe video: `video.mp4` (H.264/yuv420p re-encode, `+faststart`). |
| `sourceUrl`, `videoId` | string | Ingest provenance from job metadata (may be empty). |
| `timeline` | object | `start_ms` (0), `duration_ms`, `sample_rate_hz` — the stem sample rate (model rate, 44100 for htdemucs_6s; source video audio may be 48 kHz). |
| `video_meta` | object | ffprobe diagnostics of `video.mp4`: `codec`, `profile`, `pix_fmt`, `avg_frame_rate`, `duration`. |
| `stems` | array | Six entries, worker order `vocals, drums, bass, guitar, piano, other`. |
| `common_gain` | object | The single gain baked into all stems (see below). |
| `integrity` | object | Both gate results, profile-versioned (see below). |
| `processing` | object | Worker provenance (see below). |
| `multitrack` | string | Present only when `multi-track.mp4` was produced; its relative key. |

## `stems[]` entry

| Field | Type | Meaning |
|---|---|---|
| `id` | string | UI StemId: `vocals, drums, bass, guitar, piano, shizzle` (`other` maps to `shizzle`). |
| `name` | string | Display name (`Shizzle` for `other`). |
| `file` | string | Relative key: `stems/{stem}.m4a` (AAC, bitrate in `processing.stem_bitrate`, no resample — stays at model rate). |
| `default_gain_db` | float | Playback default in **dB** (consumers convert via `dbToLinear()` at load). `0.0` — the common gain is already baked into the encoded stems. |

## `common_gain`

Spike 0.3 policy: Demucs output preserved in float32 (Python API, no
`--clip-mode rescale` — rescale is per-stem, silent, and track-dependent and
poisons both the null gate and the mix balance). One gain, applied identically
to all six stems, chosen from `max(stem peaks, unity-sum peak)` with margin to
0.99 FS; never > 1.0.

| Field | Type | Meaning |
|---|---|---|
| `linear` | float | The applied gain (linear). |
| `db` | float | Same gain in dB. |
| `max_float_stem_peak` | float | Largest per-stem float peak before gain. |
| `max_unity_sum_peak` | float | Peak of the unity sum before gain. |

## `integrity`

Pipeline-fault detectors, **not** separation-quality scores (htdemucs_6s
nulls at only ~22.6 dB against the input even when the pipeline is perfect).
Thresholds live in `worker/integrity.py` as constants tied to
`profile_version`. The next cloud-separation work must prove them on the golden
source and representative fresh jobs before publishing those new tracks. That
upstream check does not reopen the finished 27-track delivery baseline.

```
"integrity": {
  "profile_version": 1,
  "gate_a": { ... },   // PCM reconstruction, pre-encode
  "gate_b": { ... }    // decoded-AAC delivery, post-encode
}
```

Each gate dict records: `gate`, `profile_version`, `common_gain`,
`sample_count`, `sample_count_reference`, `sample_count_signal`, `offset`,
`reference_rms_dbfs`, `rms_residual_dbfs`, `peak_residual_dbfs`,
`null_depth_db`, `thresholds{...}`, `passed`. Gate (b) adds
`length_delta_samples`.

Profile v1 pass conditions:

- **Gate (a)**: null depth >= 15 dB, residual peak <= -6 dBFS, offset == 0,
  sample counts equal. Compares unity sum of gained float stems vs the gained
  model-rate float decode of the input (identical ffmpeg decode path, so
  resampler differences are excluded).
- **Gate (b)**: null depth >= 12 dB, residual peak <= 0 dBFS, best-fit
  offset == 0, decoded length within 2048 samples (codec padding). Compares
  unity sum of decoded AAC stems vs the float remix — the exact player
  operation.

## `processing`

| Field | Type | Meaning |
|---|---|---|
| `model` | string | Demucs model (`htdemucs_6s`). |
| `segment`, `overlap`, `shifts` | numbers | Demucs parameters (7 / 0.25 / 0). |
| `stem_codec` | string | `aac`. |
| `stem_bitrate` | string | The `STEM_AAC_BITRATE` knob value (default `256k` for new lossless-derived delivery encodes). |
| `float_preserved` | bool | `true` — Python-API path, no per-stem clip handling. |
