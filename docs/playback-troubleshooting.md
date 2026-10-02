# Playback troubleshooting

Use this document to investigate an observed playback issue. For current test
commands, continuous-play/scrubbing procedures, and retained stem-codec
experiments, see [TESTING.md](TESTING.md). Recorded 27-track acceptance results
are evidence of that run, not a live inventory or a fresh deployment check.

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
| Audio cuts in and out continuously, worst on iPad | Per-stem `hardSeeks` climbing in every heartbeat while `waitingEvents` stays 0 | Synchronization and the audio-master alignment |
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
3. Check mute, solo, stem gain, and master gain first. These stem sensors are
   post-gain; silent readings can reflect intentional mixer settings as well as
   source silence. Master volume zero intentionally silences the output bus.
4. With an audible mix requested, if a stem has real PCM but the master remains
   silent, investigate the render graph and recovery state.
5. With the relevant stems unmuted and at unity, compare against the source
   passage before classifying a silent interval as a decoder or graph failure.

This distinction came from the silent intro in Mother Love Bone — Stardog
Champion. Evidence:
`evidence/cloud-continuous-playback/evidence/browser/stardog-input-pcm-replay-natural.json`.

## Synchronization and the audio-master alignment

Use when audio cuts in and out for a whole session although every stem is
buffered and no stem reports `waiting`, or when audio and picture do not
line up.

The engine never seeks or rate-changes a playing stem. On WebKit — every
iPad browser — either operation freezes the stem's clock for about a third
of a second, while a stem left alone holds a constant offset to the
millisecond (iPad probe, 2026-10-02: a seek on a playing stem lands ~350 ms
behind; six stems seeked together land 600–800 ms behind and up to 200 ms
apart; a `playbackRate` change freezes the stem 300–400 ms; correcting a
playing stem was itself the dropout — ten corrections a second produced the
continuous cutouts heard in production on 2026-10-01).

Instead, each stem's Web Audio `DelayNode` holds it back to the
latest-running stem, so all stems sound together without touching their
elements, and the silent, blob-backed video — whose seeks land quickly even
on WebKit — is seeked to the stems' audible position (`currentTime -
delayMs/1000`), aimed ahead by the lead its previous seek was measured to
lose, inside a settle/backoff/hold budget. Stem seeks remain only in the
user-seek and recovery paths, which seek the paused ensemble.

Start behaviour: a browser whose stems start together is left untouched —
no delay, no gate, audio from the first sample. A device whose starts
proved staggered remembers the delays it needed (`localStorage`) and
applies them before playback begins; when playback restarts at least
2.5 s in (resume, scrub) it rolls in from 2 s earlier with the output
silent and fades in over 250 ms finishing at the requested position, and
at the top of a song nothing is silenced. Restarts re-base every stem to
the same raw position with the remembered delays, so delays never
accumulate across restarts.

1. Read the session's `playback_events`: every event carries per-stem
   `hardSeeks`, `skewMs` (audible offset), and `delayMs`, plus top-level
   `videoSeeks`. In a healthy session `hardSeeks` stays flat after any user
   seeks, `skewMs` settles within 50 ms, and the alignment shows itself only
   as `delayMs` settling once after a start or seek and `videoSeeks` ticking
   up when a correction was needed.
2. A count that grows by more than a few per minute on every stem at once
   while `waitingEvents` stays 0 is the recovery path re-seeking the
   ensemble, not the network: read the `recovery-started` incidents for the
   reason (a stalled stem clock, a paused video, a seek that never landed)
   and investigate that subsystem.
3. `videoSeeks` growing steadily with `skewMs` never settling means the
   video's seeks are landing late or behind: the learned lead and the
   doubling backoff space the corrections (400, 800, 1600, 3200 ms…), so a
   browser in that state corrects every few seconds, not ten a second. Check
   the video's `bufferedAheadSec`/`readyState` in the same events for a
   staging or decode problem.
4. Inter-stem smear that persists (audible `skewMs` spread beyond ~40 ms
   with `delayMs` pinned at 1000) means the stems' start stagger exceeded the
   1 s delay cap; that needs the recovery path (pause/seek/resume) rather
   than the alignment.
5. Keep the existing 3-second recovery and 50 ms settled-offset limits.
   `player/e2e/audio-master-sync.spec.ts` emulates the measured WebKit
   timings in Chromium and holds the engine to zero stem seeks and zero
   `playbackRate` changes during steady playback, start, and pause/resume.

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
4. Confirm no previous audio remains audible. The current store preserves stem
   gains, mutes, solos, and master volume across track selection and reload.
   Verify that deliberate preference persistence is consistent across local and
   remote controls. Use Reset mixer to restore unity/unmuted/unsoloed stems;
   restore master volume separately.

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
