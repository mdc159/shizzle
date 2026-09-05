# RunPod lossless-stem handoff manifest

Implemented by `lossless_worker.py` and `lossless_handler.py`, packaged by
`Dockerfile.lossless`, and built by `.github/workflows/worker-image.yml`.

The RunPod worker ends by handing the VPS `lossless-stem-v1`. It produces
exactly six lossless float32 WAV stems and `handoff.json`. It does not produce
AAC browser stems or the final delivery manifest; those belong to the finished
VPS delivery pipeline.

The complete interface is
[`../interfaces/lossless-stem-v1/spec.md`](../interfaces/lossless-stem-v1/spec.md).
The machine-readable manifest definition is
[`../interfaces/lossless-stem-v1/schema.json`](../interfaces/lossless-stem-v1/schema.json).
A complete example is
[`../interfaces/lossless-stem-v1/example.json`](../interfaces/lossless-stem-v1/example.json).

## Output layout

```text
tracks/<track-id>/<generation>/separation/attempts/<sha256(idempotency_key)>/
  dispatch.json
  handoff.json
  stems/
    vocals.wav
    drums.wav
    bass.wav
    guitar.wav
    piano.wav
    shizzle.wav
```

## Stem format

- WAV container.
- IEEE 32-bit floating-point PCM (`pcm_f32le`).
- Stereo.
- 44,100 Hz.
- Sample-zero start.
- Identical sample count across all six stems.
- Original relative stem levels preserved.
- No independent normalization.
- No AAC, M4A, MP3, or other lossy derivative.

There is no bitrate setting. Each uncompressed stem is 2,822,400 b/s excluding
its small WAV header. This is a processing handoff in cloud storage, not a
browser stream.

## `handoff.json`

The manifest contains:

- Interface identifier: `lossless-stem-v1`.
- Track and generation identity supplied by the VPS job.
- Source object key and SHA-256.
- Immutable worker image identifier.
- Separation model and version.
- Sample rate, channel count, sample representation, start sample, and shared
  sample count.
- Exactly six role entries with relative file path, byte count, and SHA-256.

The worker uploads all six WAV objects first and writes `handoff.json` last.
The manifest marks a complete RunPod output package, not a published library
generation.

`dispatch.json` is a receipt written before acquisition. It records the RunPod
job ID, idempotency key, track, generation, and package prefix so the orchestrator
can reconcile an ambiguous dispatch. It is not a completion marker. The base
prefix can be supplied as `output_prefix`; the handler always appends the
attempt hash. Local mode writes `handoff.json` and `stems/` directly to its
chosen output directory and does not create a dispatch receipt.

## Worker entry points and tests

The production image copies only `lossless_worker.py`, `lossless_handler.py`,
and their shared `s3_ops.py` helper. The older `handler.py`, `audio_processing.py`,
`manifest.py`, `Dockerfile`, and `Dockerfile.reference` describe a separate
combined separation/delivery worker and are not the production image entry
point. Their existing tests still run; do not infer the deployed interface
from those files.

From the repository root, the CPU-safe tests and lint are:

```powershell
uv run --directory stemsplit pytest -q
uv run --directory stemsplit ruff check .
```

These tests mock separation and storage. They do not prove a CUDA model run,
RunPod allocation, or an end-to-end cloud publication. For the explicit local
GPU proving procedure and cloud configuration, see
[the RunPod runbook](../deploy/runpod/README.md).
