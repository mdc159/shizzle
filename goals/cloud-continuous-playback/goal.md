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
  as the authoritative browser clock.
- The player supports transport, randomized seeking, live mixing, recovery,
  natural end, and replay.
- Direct browser instrumentation observes media clocks, buffers, decoder state,
  Web Audio state, per-stem PCM, master PCM, limiter state, and recovery.
- Production telemetry identifies playback incidents without relying on a
  screenshot or external sensor.
- The complete runtime and media path is cloud-hosted and consumed through one
  standards-based browser contract.

## Delivery decision

- Preserve lossless stems as FLAC where practical; float WAV is temporary
  worker scratch only.
- New browser stems are stereo M4A/AAC-LC at one common sample rate, targeting
  44.1 kHz and 256 kb/s and never below 192 kb/s.
- Passing aligned AAC is preserved rather than lossily up-transcoded.
- All six stems receive only one common measured gain adjustment.
- New or repaired video is audio-less H.264/MP4 with browser-safe profile,
  zero-based timestamps, short closed GOP, and fast-start metadata.
- Publication is immutable: media first, manifest last, database activation
  only after the candidate passes.

The complete commands, measurements, tolerances, and rationale are retained in
`encoding-profile.md` and `evidence.md`.

## Forward use

Every future track that reaches this point with clean lossless stems uses the
same finished pipeline once. If its routine checks pass, it joins the library.
If they fail, that candidate remains outside the library.

The accepted 27-track baseline is finished work. It is not scheduled for
another development or revalidation campaign. A real future production issue
may be diagnosed and fixed when it occurs.

## Next project boundary

The active work is upstream: cloud source acquisition and dependable cloud GPU
separation that produces the clean lossless stems consumed by this completed
pipeline.
