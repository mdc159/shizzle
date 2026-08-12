# RunPod to VPS lossless-stem interface

Status: defined for every new cloud separation.

## Exact boundary

RunPod hands the VPS exactly six separated, aligned, lossless stems in the
`lossless-stem-v1` package defined here. That package is the complete interface
between cloud GPU separation and the finished VPS delivery pipeline.

```mermaid
flowchart LR
    A["URL or upload acquired in cloud"] --> B["RunPod GPU separation"]
    B --> C["Six float32 WAV stems"]
    C --> H{{"lossless-stem-v1<br/>DEFINED INTERFACE"}}
    H --> D["Finished VPS delivery pipeline"]
    D --> E["Six AAC browser stems + H.264 video"]
    E --> F["Immutable published generation"]
    F --> G["CloudFront browser playback"]

    subgraph UP["Upstream owns"]
        A
        B
        C
    end

    subgraph DOWN["Downstream owns — finished"]
        D
        E
        F
        G
    end
```

RunPod ends at this interface. The VPS begins at this interface. RunPod does
not choose browser codecs, browser bitrates, publication layout, delivery
behavior, or playback behavior.

## Required package

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

There are exactly six stem files. `other` is the separator role. The VPS maps
it to the user-facing `shizzle` role when it creates the browser generation.

## Required lossless audio format

| Property | Required value |
|---|---|
| Container | WAV |
| Audio representation | IEEE 32-bit floating-point PCM (`pcm_f32le`) |
| Sample rate | 44,100 Hz |
| Channels | 2, stereo |
| Start | Sample 0 |
| Length | Identical sample count across all six stems |
| Relative levels | Preserved exactly from separation output |
| Per-stem normalization | None |
| Lossy encoding | None |

There is no bitrate choice at this interface. Float32 WAV is uncompressed PCM:
`44,100 samples/s × 2 channels × 32 bits = 2,822,400 b/s` per stem, excluding
the small WAV header. Six stems therefore represent a fixed uncompressed audio
rate of `16,934,400 b/s`. This is a cloud processing handoff, not a browser
stream.

## Required `handoff.json`

The manifest makes the package identifiable and verifiable:

Its machine-readable JSON Schema is
[`../worker/lossless-stem-v1.schema.json`](../worker/lossless-stem-v1.schema.json).
A complete six-stem example is
[`../worker/lossless-stem-v1.example.json`](../worker/lossless-stem-v1.example.json).

```json
{
  "interface": "lossless-stem-v1",
  "track_id": "<track UUID>",
  "generation": 1,
  "source": {
    "object_key": "<private source object>",
    "sha256": "<64 lowercase hexadecimal characters>"
  },
  "separation": {
    "model": "htdemucs_6s",
    "model_version": "<model version>",
    "worker_image": "<immutable image identifier>",
    "sample_rate_hz": 44100,
    "channels": 2,
    "sample_format": "f32le",
    "start_sample": 0,
    "sample_count": 0
  },
  "stems": [
    {
      "role": "vocals",
      "file": "stems/vocals.wav",
      "bytes": 0,
      "sha256": "<64 lowercase hexadecimal characters>"
    }
  ]
}
```

The real manifest contains exactly one stem entry for each required role. The
job writes the real sample count, byte counts, hashes, worker image identifier,
and model version.

## Handoff sequence

1. RunPod separates the source into the six required roles.
2. RunPod writes the exact float32 outputs to the required WAV paths without
   changing their relative levels.
3. RunPod uploads all six WAV files.
4. RunPod writes `handoff.json` last.
5. The VPS receives the complete package and runs the finished delivery
   transformation with no song-specific alternate path.

A partial or differently formatted result has not crossed the interface. A
complete `lossless-stem-v1` package has crossed it.

## Fixed downstream transformation

The VPS transforms every accepted package the same way:

1. Calculate at most one common attenuation value for the complete six-stem
   set; never normalize stems independently.
2. Derive six stereo AAC-LC/M4A browser stems at the fixed new-encode target of
   256 kb/s per stem.
3. Derive or copy the browser-safe audio-less H.264 video.
4. Require the complete browser generation to remain at or below 2.5 Mb/s
   average.
5. Run the established artifact checks.
6. Publish the immutable generation and activate it.
7. Deliver through CloudFront and the finished browser player.

No downstream format decision is delegated back to RunPod.

## Measured downstream envelope

The accepted 27-track production library establishes the delivery envelope:

| Measurement | Accepted-library result |
|---|---:|
| Browser stems measured | 162 |
| AAC stem average bitrate range | 41,120–295,166 b/s |
| AAC stem median average bitrate | 187,970 b/s |
| Complete track average bitrate range | 1,472,888–2,331,333 b/s |
| Complete track ceiling | 2,500,000 b/s |
| The Pot complete average bitrate | 1,767,869 b/s |

The low individual AAC averages occur on low-information or nearly silent
stems. They do not lower the encoder target and do not create a separate path.
The fixed input remains lossless float32 WAV; the fixed delivery target remains
AAC-LC at 256 kb/s per stem with the complete track capped at 2.5 Mb/s.

## Responsibility split

| RunPod separation side | VPS delivery side |
|---|---|
| Receive the cloud source object | Receive `lossless-stem-v1` |
| Run `htdemucs_6s` | Apply the fixed delivery transformation |
| Produce six float32 stem arrays | Calculate one common attenuation if needed |
| Write six aligned float32 WAV files | Encode six AAC-LC/M4A browser stems |
| Record object hashes and separation identity | Derive or copy browser-safe video |
| Write `handoff.json` last | Audit and publish an immutable generation |
| Stop at the interface | Deliver through CloudFront and play in browser |

## Next implementation target

The next RunPod implementation is complete when an ordinary cloud-acquired
source reliably produces this exact package. Once the package crosses the
interface, the existing VPS delivery process takes over without redesign or
per-track exceptions.
