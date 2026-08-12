# Goal: reliable cloud playback for the Shizzle library

Status: complete.

## Goal

Starting with clean aligned lossless stems, produce immutable browser-ready
track generations that play, seek, mix, recover, end, and replay reliably from
the 100% cloud-hosted Shizzle application.

## Completed result

- The production library contains exactly 27 accepted tracks.
- Every active track passes the versioned `shizzle-browser-v1` artifact profile.
- Every track exposes the six required roles: vocals, drums, bass, guitar,
  piano, and shizzle.
- Media is private in S3 and delivered through signed CloudFront URLs.
- Six AAC stems stream by Range request; one bounded audio-less video is staged
  as the browser master clock.
- The player supports transport, randomized seeking, live mixing, recovery,
  natural end, and replay.
- Direct browser instrumentation observes media clocks, buffers, decoder state,
  Web Audio state, per-stem PCM, master PCM, limiter state, and recovery.
- Production telemetry identifies playback incidents without relying on a
  screenshot or external sensor.
- The complete runtime and media path is cloud-hosted and consumed through one
  standards-based browser contract.

## Delivery decision

- The RunPod input boundary is `lossless-stem-v1`: six stereo 44.1 kHz IEEE
  float32 WAV stems with one start sample and one sample count.
- Every new lossless-derived browser stem uses stereo M4A/AAC-LC at 44.1 kHz
  with a 256 kb/s encoder target. Actual AAC average bitrate varies with the
  audio.
- Every new complete browser generation must remain at or below 2.5 Mb/s
  average.
- Passing aligned AAC is preserved rather than lossily up-transcoded.
- All six stems receive only one common measured gain adjustment.
- New or repaired video is audio-less H.264/MP4 with browser-safe profile,
  zero-based timestamps, short closed GOP, and fast-start metadata.
- Publication is immutable: media first, manifest last, database activation
  only after the candidate passes.

The complete commands, measurements, tolerances, and rationale are retained in
`encoding-profile.md` and `evidence.md`.

## Forward use

Every future `lossless-stem-v1` package uses the same finished pipeline once.
There is no track-specific alternate path downstream of that interface.

The accepted 27-track baseline is finished work. It is not scheduled for
another development or revalidation campaign. A real future production issue
may be diagnosed and fixed when it occurs.

## Next project boundary

The active work is upstream: cloud source acquisition and dependable cloud GPU
separation that produces the exact `lossless-stem-v1` package consumed by this
completed pipeline.
