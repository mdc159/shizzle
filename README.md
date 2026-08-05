# Shizzle — Cloud Karaoke Stem Mixer

Shizzle turns a song into six independently mixable stems and plays them from
the cloud in a standards-compliant browser.

## Current status

The browser-delivery and playback portion is finished.

- The production library contains 27 accepted tracks.
- All 27 use audited immutable generations that satisfy `shizzle-browser-v1`.
- Playback, seeking, live mixing, synchronization, recovery, replay, direct
  health sensing, and cloud media delivery have been proven on the library.
- The runtime is entirely cloud-hosted and exposes one standards-based browser
  media path.

The remaining work is upstream ingestion: reliably turning a new URL or upload
into clean lossless six-stem material using cloud infrastructure.

## Process flow

```text
URL or upload
    ↓
Cloud source acquisition
    ↓
Cloud GPU separation
    ↓
Six clean lossless stems
    ↓
FINISHED DELIVERY PIPELINE
  encode → verify → publish immutable generation
    ↓
27-track library + future accepted tracks
    ↓
CloudFront → any conforming browser
```

The boundary is simple: once six clean, aligned lossless stems reach the
finished delivery pipeline, Shizzle has a repeatable path to browser-ready
media. A new track runs through that path once and is admitted if its routine
checks pass. The existing 27-track baseline is not reprocessed or subjected to
another development campaign. This portion is revisited only if an actual
problem appears.

## Finished delivery profile

- Upstream preservation: FLAC where practical; float WAV may be used as
  temporary worker scratch.
- Browser stems: six stereo M4A/AAC-LC files; new encodes target 44.1 kHz at
  256 kb/s and never fall below 192 kb/s.
- Video: audio-less H.264/MP4, browser-safe profile, zero-based timestamps,
  short closed GOP, and fast-start metadata.
- Publication: immutable `tracks/<track-id>/<generation>/` objects, media first,
  `manifest.json` last, then one database pointer activation.
- Delivery: private S3 through signed CloudFront URLs; six stems stream by
  Range request and one bounded video is staged into a revocable browser Blob.
- Playback health: direct media clocks, buffers, decoder state, Web Audio state,
  per-stem PCM, master PCM, limiter state, recovery, and first-party telemetry.

The exact frozen profile and measurements are in
[`goals/cloud-continuous-playback/encoding-profile.md`](goals/cloud-continuous-playback/encoding-profile.md).

## What to work on next

1. Make the RunPod worker dependable on cloud GPU hardware.
2. Connect URL/upload ingestion to that worker.
3. Produce and verify six clean lossless stems.
4. Hand those stems to the finished delivery pipeline.

Do not redesign or revalidate the finished playback portion while doing this.

## Repository layout

```text
server/    FastAPI API, database, orchestration, publication, and telemetry
worker/    Cloud GPU separation and media preparation
ui/        Browser player and six-stem mixer
infra/     VPS, Caddy, and deployment configuration
scripts/   Auditing, migration, and operational tools
goals/     Finished playback profile and retained evidence
spikes/    Playback troubleshooting experiments and engineering evidence
docs/      Current design, handoff, provenance, and incident notes
```

## Current documentation

| Document | Purpose |
|---|---|
| [`docs/HANDOFF.md`](docs/HANDOFF.md) | Current live state and next upstream work |
| [`docs/superpowers/specs/2026-08-02-shizzle-cloud-karaoke-design.md`](docs/superpowers/specs/2026-08-02-shizzle-cloud-karaoke-design.md) | Current architecture and simple workflow |
| [`specs/shizzle-cloud-karaoke-implementation.html`](specs/shizzle-cloud-karaoke-implementation.html) | One-page visual implementation flow |
| [`goals/cloud-continuous-playback/encoding-profile.md`](goals/cloud-continuous-playback/encoding-profile.md) | Finished browser-delivery protocol |
| [`goals/cloud-continuous-playback/evidence.md`](goals/cloud-continuous-playback/evidence.md) | Evidence that produced the finished protocol |
| [`docs/playback-troubleshooting.md`](docs/playback-troubleshooting.md) | Targeted diagnostics and retained playback experiments |
| [`docs/library-source-purge-2026-08-05.md`](docs/library-source-purge-2026-08-05.md) | Documentation-only note about purged source material |

## Configuration

Copy `.env.example` to `.env` and fill in the documented values. Never print or
commit secrets. On machines with a global `AWS_ENDPOINT_URL`, clear that value
before accessing the production AWS account so commands do not silently target
another S3-compatible service.
