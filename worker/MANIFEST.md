# RunPod lossless-stem handoff manifest

Status: defined target output for the next RunPod implementation.

The RunPod worker ends by handing the VPS `lossless-stem-v1`. It produces
exactly six lossless float32 WAV stems and `handoff.json`. It does not produce
AAC browser stems or the final delivery manifest; those belong to the finished
VPS delivery pipeline.

The complete interface is
[`../docs/lossless-stem-handoff.md`](../docs/lossless-stem-handoff.md).
The machine-readable manifest definition is
[`lossless-stem-v1.schema.json`](lossless-stem-v1.schema.json).
A complete example is
[`lossless-stem-v1.example.json`](lossless-stem-v1.example.json).

## Output layout

```text
tracks/<track-id>/<generation>/separation/
  handoff.json
  stems/
    vocals.wav
    drums.wav
    bass.wav
    guitar.wav
    piano.wav
    other.wav
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

## Current implementation gap

The present worker code continues past separation into AAC encoding, video
derivation, and a delivery-shaped manifest. The next RunPod work changes that
implementation to stop at `lossless-stem-v1`. The VPS then consumes the
lossless package and runs the existing `shizzle-browser-v1` process with no
song-specific alternate path.
