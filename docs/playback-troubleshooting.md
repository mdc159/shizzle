# Playback troubleshooting

Use this document only when an actual playback issue is observed. The 27-track
library and the delivery pipeline are finished; these procedures are not a
standing acceptance campaign.

## Start here

1. Record the track id, active generation, application build, browser/version,
   timestamp, and visible error.
2. Read `window.__shizzlePlaybackHealth.getMetrics()` immediately.
3. Query first-party playback incidents for that track and generation.
4. Identify whether the problem is the source artifact, CDN delivery, a media
   decoder, synchronization, the Web Audio graph, or track transition state.
5. Run only the targeted procedure below.
6. Expand beyond the affected track only if the evidence indicates a shared
   system defect.

Do not begin by replaying or reprocessing the entire library.

## Direct signals

The browser health object is the primary sensor. Capture:

- Video time, state, ready state, buffered ranges, source type, and media error.
- Each stem's time, state, ready state, buffered ranges, and media error.
- Maximum inter-stem skew and stem-to-video offset.
- `AudioContext` state.
- Per-stem post-gain PCM levels.
- Post-limiter master PCM level and limiter reduction.
- Current recovery state, attempt, reason, and elapsed time.
- Track id, active generation, manifest profile, and application build.

A spinner, screenshot, elapsed wall clock, or one advancing element is not
enough to locate a playback problem.

## Symptom guide

| Symptom | Check first | Targeted procedure |
|---|---|---|
| “Playback failed” or frozen video | Video media error, staged Blob size, source generation, incident reason | Video decode and staging |
| One or more stems stop | Stem clock/ready state/buffer/error and aborted Range incidents | Stem delivery and recovery |
| Video advances but output is silent | Per-stem PCM, master PCM, `AudioContext`, mute/solo/gain state | Silent graph versus real source silence |
| Audio sounds doubled or smeared | Inter-stem skew, stem/video offset, duplicate elements/nodes | Synchronization and transition leak |
| Seek hangs or resumes out of sync | Seek target, buffered ranges, recovery time, final settled offsets | Random-seek reproduction |
| Next song contains previous audio or mixer settings | Active media elements, node/timer counts, Blob revocation, mixer state | Sequential transition test |
| Clipping, pumping, or gain jump | Decoded true peak, limiter reduction, common gain, fader state | Audio-quality check |
| Media returns 403 or will not seek | Signed URL expiry, CORS, `Accept-Ranges`, 206 response | CloudFront authorization and Range |

## Video decode and staging

Use when the video freezes, fails to start, or throws `MEDIA_ERR_DECODE`.

1. Confirm the active manifest points to the expected immutable generation.
2. Verify the video is audio-less H.264/MP4, zero-based, fast-start, and within
   the 128 MiB staging limit.
3. Fetch the exact signed object and confirm a complete decode with FFmpeg.
4. Inspect keyframe gaps and timestamps.
5. Confirm the browser reports a nonzero staged byte count and a `blob:` video
   source before Play becomes available.

The Pot originally failed because its video had end-loaded metadata, roughly
seven-second keyframe gaps, and a complete-decode error. The repaired short-GOP,
fast-start video removed the production seek failure. See
`evidence/cloud-continuous-playback/evidence.md` sections “What failed and why” and
“What changed.”

## Stem delivery and recovery

Use when a stem stalls, disappears, or fails after seeking.

1. Confirm each stem URL returns CORS headers and HTTP 206 for a byte-range
   request.
2. Compare the stopped stem's clock, buffer, ready state, and media error with
   the other five stems.
3. Reproduce by aborting one real AAC Range request for the affected track.
4. Require automatic recovery to the same generation with settled offsets no
   greater than 50 ms and recovery no longer than 3 seconds.

The retained 27-track bounded-fault report is
`evidence/cloud-continuous-playback/evidence/browser/library-27-faults-staged-video.json`.
Use its procedure against the affected track, not as a default whole-library
run.

## Silent graph versus real source silence

Use when the picture advances but no audio is heard.

1. Read every post-gain stem PCM level.
2. Read post-limiter master PCM.
3. If all stem inputs are silent, treat the interval as source silence.
4. If a stem has real PCM but the master remains silent, treat it as a broken
   render graph and invoke recovery.
5. Verify mute, solo, stem gain, and master gain state before rebuilding nodes.

This distinction came from the silent intro in Mother Love Bone — Stardog
Champion. Evidence:
`evidence/cloud-continuous-playback/evidence/browser/stardog-input-pcm-replay-natural.json`.

## Random-seek reproduction

Use for a specific seek or synchronization report.

1. Use a recorded seed and at least several targets spanning early, middle, and
   late playback.
2. Record requested target, video time, all six stem times, settled offsets,
   buffer state, recovery actions, and time to healthy playback.
3. Exercise mixer controls while playing, then seek again to expose stale state.
4. Keep the existing 3-second recovery and 50 ms settled-offset limits.

The full development stress procedure is retained in
`evidence/cloud-continuous-playback/evidence/browser/library-27-stress-staged-video.json`.
Use only the relevant track or a small representative set unless the defect is
demonstrably system-wide.

## Sequential transition test

Use when audio, controls, or memory leak between songs.

1. Play the affected track, change several mixer controls, and switch tracks.
2. Confirm the previous video Blob is revoked.
3. Confirm previous media elements, Web Audio nodes, timers, watchdog state, and
   recovery work are disposed.
4. Confirm the new track begins with its own default mixer state and no previous
   audio remains audible.

The original one-session 27-track stress run exercised this path; repeat only
the smallest sequence that reproduces the issue.

## Audio-quality check

Use when a new track or incident sounds clipped, pumped, phasey, imbalanced, or
incorrectly separated.

1. Re-run decoded default-mix sample safety and true-peak measurement.
2. Confirm every stem received the same recorded gain change.
3. Inspect steady-state limiter reduction while direct PCM is expected.
4. Listen to the default mix, vocals-muted mix, individual roles, the reported
   passage, and one transition.
5. Use `evidence/cloud-continuous-playback/listening-worksheet.md` to record the
   affected track only.

Do not transcode an already-lossy file merely to increase its nominal bitrate.

## CloudFront authorization and Range

1. Confirm an unauthenticated object request fails.
2. Obtain a fresh authenticated manifest.
3. Confirm each signed file URL returns the correct object.
4. Send a small Range request and require HTTP 206 with the expected bytes.
5. Confirm CORS permits the production application origin and credentials are
   absent from durable telemetry.

The original signed-delivery experiments are retained in
`evidence/spikes/RESULTS-0.2.md` and `evidence/spikes/signed-cookie-proof/`.

## Retained experiment index

- `evidence/cloud-continuous-playback/evidence.md` — why the final playback design
  changed and what each failure taught.
- `evidence/cloud-continuous-playback/evidence/browser/` — machine-readable natural,
  stress, fault, recovery, replay, and transition reports.
- `evidence/cloud-continuous-playback/evidence/vps/` — artifact, publication,
  activation, audio-quality, and delivery reports.
- `evidence/spikes/RESULTS-0.1.md` — early decoder-clock/skew experiment.
- `evidence/spikes/RESULTS-0.2.md` — signed media and Range experiment.
- `evidence/spikes/RESULTS-0.3-0.4.md` — Demucs gain/reconstruction and AAC comparison.
- `evidence/spikes/RESULTS-frontend-deploy.md` — early outside-in deployment checks.

These records are troubleshooting assets. They explain observed failure modes
and provide reproducible targeted tests; they are not unfinished requirements.
