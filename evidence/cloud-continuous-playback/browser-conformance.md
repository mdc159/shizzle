# Browser contract

Status: accepted.

Shizzle exposes one standards-based browser contract defined by required web
capabilities rather than client hardware or vendor-specific media routes.

## Required capabilities

- HTML media elements with AAC-LC/M4A and H.264/MP4 decode.
- `AudioContext`, `MediaElementAudioSourceNode`, `GainNode`,
  `DynamicsCompressorNode`, and `AnalyserNode`.
- HTTPS, CORS, HTTP byte ranges, `fetch`, `Blob`, and object URLs.
- Promises, typed arrays, high-resolution timing, and Page Visibility events.

The application feature-detects required capabilities and must report a clear
unsupported-browser reason rather than starting a partial playback graph.

## Delivery behavior

Every conforming browser:

1. Loads the UI and API over HTTPS.
2. Receives expiring file-scoped CloudFront URLs.
3. Streams six AAC stems with CORS and byte ranges.
4. Stages one bounded audio-less H.264 video as the authoritative clock.
5. Mixes through the same Web Audio graph.
6. Emits the same direct health and telemetry schema.

Production Chromium supplied the deterministic full-library stress and fault
evidence used to complete the delivery work. That is implementation evidence,
not a Chromium-only architecture. No separate browser-engine campaign remains
open. If a standards-compliant browser later exposes an actual defect, diagnose
and fix that observed defect without creating a device-specific media path.

Browser automation is only a consumer of `https://shizzle.systems`; it never
hosts, proxies, processes, or supplies application media.
