# Browser delivery profile

Profile: `shizzle-browser-v1`, version 1. Executable policy:
[`delivery_profile.py`](../../library/src/shizzle_server/publish/delivery_profile.py).
Lossless input: [`lossless-stem-v1`](../lossless-stem-v1/spec.md).

## Current format and gates

| Concern | Implemented policy |
|---|---|
| Roles | Exactly vocals, drums, bass, guitar, piano, shizzle |
| New audio | Stereo AAC-LC in M4A, 44.1 kHz, encoder target 256 kb/s, fast-start |
| Existing audio | Compatible aligned 44.1 or 48 kHz AAC may be copied without lossy up-transcoding |
| Audio average bitrate | Below 192 kb/s is currently an error for new material, warning for preserved existing AAC; see the limitation below |
| New video | Audio-less H.264 Main Level 3.1, yuv420p, bounded to 1280x720 at 30 fps, CRF 20, initial 1200k max rate, fast-start |
| Copied video | Main/High and compatible rates 23.976, 24, 25, 29.97, 30, 59.94 fps, subject to the other gates |
| Timeline start | Within 0.020 seconds of zero |
| Common stem tail/duration | Within 0.080 seconds of declared duration |
| Inter-stem spread | At most 0.005 seconds |
| Video duration | Within 0.100 seconds of declared timeline (D1) |
| Keyframe gap | At most 2.05 seconds; new encoding uses a two-second GOP |
| Complete generation | At most 2,500,000 b/s average |
| Publication guard | Stems must be M4A and no larger than 64 MiB each |
| Browser video staging | Reject declared or resulting Blob size over 128 MiB |
| Gain | At most one common attenuation, never independent stem normalization or a boost; -1.0 dBTP policy ceiling |

The 256 kb/s value is an encoder target, not a guaranteed measured average.
Sparse or silent AAC compresses to a lower bitrate. The current new-encode
minimum rejects valid sparse stems; [Review](../../docs/REVIEW.md) tracks the
policy/code mismatch for a reviewed fix.

The lossless intake measures the lossless unity mix and records a common
`default_gain_db` on every output stem. `encode_stem()` does not bake that gain
into the AAC. The player combines that manifest trim with the user mixer gain
(rendered gain = trim + fader, in dB; issue #25), so store sync, Reset mixer,
and remote mixer updates can never overwrite it. The separate
decoded-delivery audio-quality audit measures the mix using supplied gains.
Do not treat a passed artifact probe as a listening test or a proof of
post-AAC mix safety.

## Object layout and manifest

```text
tracks/<track-uuid>/<generation>/
  manifest.json
  video.mp4
  stems/
    vocals.m4a
    drums.m4a
    bass.m4a
    guitar.m4a
    piano.m4a
    shizzle.m4a
```

The v3 manifest records title, artist, duration, video and stem paths,
`default_gain_db`, common timeline, delivery-profile details and integrity/
derivation provenance. The exact generated shape is in
[`lossless_intake.transform`](../../library/src/shizzle_server/publish/lossless_intake.py).
The player-visible types are in
[`karaoke.ts`](../../player/src/types/karaoke.ts).

Package checks prove the input hashes, sizes, format and sample alignment.
Candidate audits hash and fully decode the seven browser artifacts, inspect
timestamps/keyframes/fast-start and enforce the profile. The transform reduces
video bitrate once if necessary to meet the complete-generation budget, then
rejects a candidate still over budget. New cloud intake derives video;
migration can copy already compatible media.

Staging is verified before promotion. Media is copied to an immutable generation
and `manifest.json` is written last. A completed destination generation is
not overwritten. New ingestion publishes the track row and job readiness
together; later migrations activate a candidate through the generation
compare-and-swap/event ledger. See [Architecture](../../docs/architecture.md).

## Derivation reference

The implementation is authoritative; these commands describe its codec choices.
Use aligned lossless inputs for each stem:

```text
ffmpeg -i aligned-stem.wav -map 0:a:0 -c:a aac -profile:a aac_low -b:a 256k -ar 44100 -ac 2 -movflags +faststart stem.m4a
```

Video derivation in `derive_video()` additionally trims to the stem duration:

```text
ffmpeg -fflags +genpts -i source-video -map 0:v:0 -an -t <stem-duration> -vf "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30,format=yuv420p,setpts=PTS-STARTPTS" -c:v libx264 -preset medium -crf 20 -maxrate 1200k -bufsize 2400k -profile:v main -level:v 3.1 -g 60 -keyint_min 60 -sc_threshold 0 -movflags +faststart -video_track_timescale 90000 video.mp4
```

Do not independently resample mismatched stems or up-transcode existing lossy
audio to make a metadata target appear satisfied. Record input/output hashes,
tool versions and measured results with each acceptance run.

## Delivery and playback

The authenticated `GET /api/tracks/{id}/manifest` returns file-scoped expiring
CloudFront URLs (default 24 hours). CloudFront fronts private S3 with origin
access control and Range/CORS support. The six AAC elements stream directly
from the edge. After stem readiness, the browser fetches the audio-less video
and uses one revocable Blob as the master timeline. Old Blobs are released
on track change. The size checks do not establish a strict streaming memory
bound when response length is missing: the body is converted to a Blob before
the post-download check.

Per-stem clock and PCM measurements, output PCM, AudioContext state, compressor
reduction and recovery incidents are available through the read-only health
hook. No signed query credentials belong in telemetry or retained evidence.

## Playback, stem optimization and listening acceptance

[Testing](../../docs/TESTING.md) retains the full procedure: natural playback,
long and repeated backward/forward seeks, mute/solo/fader changes, bounded
delivery faults, recovery, natural end/replay, codec comparisons, gain/null
experiments, and listening worksheets. [Troubleshooting](../../docs/playback-troubleshooting.md)
maps symptoms to those checks.

AAC delivery keeps six simultaneous streams practical; lossless float32 stays
upstream for processing and re-derivation. The retained experiments compare AAC
renditions and gain behavior, and explain why one staged video improves the
master clock while audio remains streamed. Measurements and prior acceptance
results live under [playback evidence](../../evidence/cloud-continuous-playback/evidence.md);
they are dated test results, not current inventory or guarantees for every browser.
