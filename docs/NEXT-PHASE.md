# Next phase

Written 2026-10-02, after the audio-master sync change (PR #60, `196ac88`)
and the documentation pass (PR #61). This is the owner's plan and a map
of what is unknown. Nothing in it has been started.

Every claim is one of three kinds, marked so the reader can tell them
apart: a measured or verified fact, with its number, date and source; the
owner's stated preference, quoted as he said it on 2026-10-02; or a
working default chosen to keep moving, marked "(working default)".

## Philosophy: audio first

The owner, 2026-10-02:

> "If I describe this app as audio first, audio playback is the most
> important thing with video definitely being secondary. The object would
> be that the audio plays without incident on all surfaces."

On trading a video glitch against an audio one at the start of a song:

> "If I had the option of, at the beginning of the song, the first second,
> get a little video jiggle or the audio warble, I'll take the video jiggle
> every time."

He clarified that this is a one-time trade at the top of a song, not a
video loose all the way through. On the goal itself:

> "Is there really no way to just have a master clock and everything syncs
> from the beginning? This doesn't seem like something we should have to
> compromise."

The rule that follows: when a correction has to happen, it goes on the
video, never on playing audio. The best outcome is that nothing needs
correcting at all.

## Where sync stands today

[playback-troubleshooting.md](playback-troubleshooting.md) holds the
diagnosis procedure and the retained evidence. The measurements, in order:

- August 2026 design: six `<audio>` stems plus one audio-less `<video>`,
  the video clock as master, stems seeked and rate-nudged toward it.
  Spike 0.1 on desktop Chrome: 3 ms inter-stem skew over 190 s, one
  startup seek, zero nudges. Library acceptance 2026-08-05 on production
  Chromium: 27/27 tracks, maximum stem/video skew 28 ms. The iPad row of
  that spike was marked "PENDING HUMAN RUN" and never run.
- 2026-10-02, the owner's iPad (every iPad browser is WebKit): seeking
  one playing stem lands ~350 ms behind and freezes the stem meanwhile;
  seeking all six lands 600-800 ms behind with up to 200 ms spread; a
  `playbackRate` change freezes a stem for 300-400 ms; stems start 16 ms
  apart (80 ms across six); left alone they do not drift; the silent
  video seeks in 25 ms. Desktop Chromium: 1-4 ms for all of these.
- PR #59 (budgeted re-seeks), in the owner's words: "a slight
  improvement, but it's still cutting out".
- PR #60 (merged 2026-10-02, `196ac88`): audio-master alignment. Each
  stem runs through a per-stem Web Audio `DelayNode`; early stems are
  delayed to the latest-running one; the silent video is moved to the
  stems' audible position; the sync loop never seeks or rate-changes a
  playing stem; delays are remembered in `localStorage`
  (`shizzle_stem_start_delays`) and applied before the first sample on
  later plays; a resume or scrub at least 2.5 s in rolls in from 2 s
  earlier behind a fade; desktop Chromium behaviour is unchanged. iPad
  result: zero stem seeks, all six stems 23 ms from the video with zero
  spread for the rest of the song.
- The remaining audible defect: on the first play on a device with no
  remembered pattern, the delays land about 1.4 s in, on stems that are
  already playing — the "warble". It is stem-to-stem alignment, not
  audio-to-video, so the video cannot absorb it. The only free fix is to
  set the delays before the first sample.

Still true in the engine (verified 2026-10-02 in
`player/src/lib/playback/mediaElementEngine.ts`):

- `video-clock-stalled`, `video-buffering` and `video-media-error`
  incidents call `recover()`, which pauses and re-seeks every stem: a
  video problem becomes an audio gap.
- Playback waits for the staged video Blob before starting.
- One non-coordinated recovery path (`hardSyncToVideo`) still seeks
  playing stems.
- There is no handler for an iOS audio-session interruption; the owner
  saw `InvalidStateError: Failed to start the audio device` once.

## Options for a single clock

The owner's real question is the third quote above. Three options,
cheapest first.

### a. One 12-channel audio file per track

Six stereo stems muxed into one file as one track: one element, one
decoder, one clock. A `ChannelSplitterNode` feeds the six faders. Sync
becomes exact because the stems share a timeline by construction.

Risk: browsers may downmix audio with more than two channels to stereo
inside `MediaElementAudioSourceNode` — Safari on stereo-output devices
is the likely offender — which would merge the stems into one mix. Codec
support for 12-channel AAC versus Opus in CAF/WebM on Safari is also
open.

Test: hand-build one 12-channel file, serve it from one page, play it on
the iPad, the Mac, Chrome and Firefox; check the `channelCount` the
splitter reports and that soloing one stem isolates it.

Cost: a day for the spike. If it works, a new delivery generation per
track and a small engine change.

### b. One scheduler clock in the browser

Fetch the stem bytes, decode them — WebCodecs `AudioDecoder` with a JS
MP4 demuxer against the existing `.m4a` files, or `decodeAudioData` on
server-cut segments — and schedule `AudioBufferSourceNode`s on the
`AudioContext` clock. Sync is exact from the first sample on every
browser; read-ahead is under our control (the owner asked "why can't it
read ahead?"); seeks are instant; this is the only design that supports
future key-change and tempo features; the video becomes a pure follower.

Risks: pure Web Audio playback stops when an iPad locks its screen,
while media elements keep playing; AirPlay routing of Web Audio output
on Safari is historically unreliable; decoded PCM is large — a full
decode of six 4-minute stereo stems is roughly 500 MB of float32, so
playback must be segmented or streamed, not decoded whole.

Cost: a new audio engine, with the recovery and health machinery
rebuilt around it.

### c. Keep six elements, preset the delays

What exists, plus a first-play profile: apply the measured WebKit stagger
(80/64/48/32/16/0 ms) before the first sample on a device with no
remembered pattern, then confirm from the clocks and hold any correction
until the next pause or seek. Smallest change; still six clocks
underneath; a wrong preset is itself a smear.

### Order and small items regardless of option

Spike (a) first, because it is an afternoon on the iPad and either works
or does not. Try (b) if (a) fails, or when pitch/tempo features are
wanted. (c) is the fallback that keeps what exists.

1. Video incidents must never touch audio.
2. Audio starts when the stems are ready; the video joins when staged.
3. Stems are only seeked while paused. Make this a numbered invariant in
   [INVARIANTS.md](INVARIANTS.md) with a guarding test.
4. Handle `AudioContext` `statechange` and interruption, and show "tap
   to resume".

## How to check ~80% of the surfaces

The owner, 2026-10-02: check "80% of the browsers or potential options".
Status of each surface:

| Surface | Status |
|---|---|
| Windows Chrome | measured 2026-10-02 |
| iPad Safari | measured 2026-10-02 (WebKit) |
| iPad Chrome | measured 2026-10-02 (WebKit) |
| Mac Safari | not measured |
| Android Chrome | not measured |
| Firefox desktop | not measured |
| Edge | not measured; counts as Chromium |
| Samsung Tizen projector (August spike) | not measured |
| AirPlay from iPad to a TV | not measured |
| Bluetooth speaker from iPad | not measured |
| iPad screen lock | not measured |
| An interruption, such as a phone call | not measured |
| Headphones plugged in mid-song | not measured |
| A 7-minute track on a 48 kHz output route | not measured |

Prioritised by likely use: iPad, Mac Safari, Windows Chrome/Edge,
Android Chrome, AirPlay-to-TV, Bluetooth. Those six are the 80%.

The harness (verified 2026-10-02):

1. `npm run build` in a worktree.
2. `vite preview --host 127.0.0.1 --port 5193 --strictPort` with
   `SHIZZLE_API_PROXY=https://shizzle.systems` and
   `__VITE_ADDITIONAL_SERVER_ALLOWED_HOSTS=<tailnet host>`.
3. `tailscale serve --bg --https=8446 http://127.0.0.1:5193`.
4. Open it on the device and play a full song.
5. Read `playback_sessions` and `playback_events` in the production
   Postgres: per-stem `hardSeeks`, `skewMs` and `delayMs`, plus
   top-level `videoSeeks` and recoveries.

Pass criterion per surface (working default): over one full song, zero
stem seeks after start, zero rate changes, zero recoveries, inter-stem
audible spread under 40 ms, and no audible gap reported by the listener.
Define "incident" as audible; picture offset alone is not an incident.

## What could trip us up

iOS behaviours:

- Screen lock: media elements keep playing, pure Web Audio stops (this
  decides option b); the current engine's behaviour under lock is
  untested.
- Audio interruptions (a phone call): no handler exists; one
  `InvalidStateError: Failed to start the audio device` was seen.
- The mute switch: its effect on this app is unverified.

Output routes:

- AirPlay from an iPad is unmeasured, and Safari's routing of Web Audio
  output over AirPlay is historically unreliable.
- Bluetooth adds 100-250 ms of latency; iOS does not report
  `outputLatency` reliably, so the fix is a video offset or a user
  lip-sync slider.
- A route change can reset the output sample rate (unverified on this
  app).

Engine and presets:

- A wrong preset delay (option c) is itself a smear, not just a missed
  fix.
- The health detectors can cause the incidents they guard against:
  `render-silence` firing on a quiet intro, `stem-clock-stalled`
  restarting the ensemble over a 200 ms hiccup; the thresholds were
  tuned on Chromium.

Content and delivery:

- The no-drift measurement covers 43 s; a 7-minute track is untested.
- Signed CloudFront URLs expire after 24 h (`media_ttl_seconds`). Fine
  for a pause; anything longer needs a fresh manifest.
- The surfaces listed above that are still unmeasured.

Doors that close:

- If browsers downmix multichannel audio, option a is closed on those
  browsers.
- Key-change and tempo features need option b; a and c cannot carry
  them.

## Experimenting on the library: cost and where the stems live

Verified 2026-10-02 against the production bucket and database:

- 28 live tracks. Only about 3 have lossless stems in S3: one
  cloud-pipeline track with six WAVs under
  `tracks/<id>/1/separation/attempts/<sha>/stems/*.wav` (109 MB each,
  ~650 MB per song), and two legacy `karaoke/<job>/` records that
  include WAVs. The other ~25 came through the legacy importer and exist
  in the cloud as AAC `.m4a` stems and video only.
- Bucket total ~21 GB: `tracks/` 12.6 GB, `karaoke/` 8.5 GB, `sources/`
  0.5 GB. The 62 MP4s under `karaoke/` look like the original source
  videos.
- The pipeline's asset is the `lossless-stem-v1` package; the browser
  package is a derivative. The schema has generations, migration and
  audit tooling, atomic activation and rollback
  (`library/src/shizzle_server/publish/library_migration.py`), so a new
  delivery format is a new generation per track, no GPU involved,
  wherever the WAVs exist.

The owner, 2026-10-02:

> "the motivation to fix this now would be that the library is small,
> only 28 songs. If we have to trash everything and start over, it's only
> 28 songs."

> "The rewriting of the file is pretty quick. It's the stem separation
> that's the heavy part."

> "For experimentation, as long as the Dell PC, the 9530, is up with the
> 4070 GPU, we just use it, and/or the Mac, because I think we'd
> established they both separate stems equally."

> "The 650 megabyte per song — I don't know if I want to pay for that on
> S3. We could probably store those locally on some drive."

For context (working estimate): 28 x 0.65 GB is about 18 GB, which is
about $0.40/month at standard S3 pricing. Following the owner's
preference (working default): lossless packages for experiments live on a
local drive, as a `lossless/<track-id>/` tree mirroring the package
layout (the path is a placeholder); separation runs on the Dell 9530
(RTX 4070) or the Mac; only derived browser packages go to S3.

To regain lossless stems for the ~25 legacy tracks:

1. First check whether the k25 pipeline's WAVs still exist on a local
   machine; the August spike referenced `X:\GitHub\k25\data\<job>`.
2. Otherwise re-separate from the source videos locally.
3. Re-encoding the AAC stems is acceptable only for a throwaway spike
   (lossy to lossy).

Related prior thinking: `docs/NODE_ROUTING_PLAN.md` (2026-09-24, in the
main checkout, not committed) describes routing URL-sourced jobs to local
nodes (Mac mini, Dell 9530, Dell M6800). It is related work, not a
decision.

## Sharing with a few friends: what breaks first

Verified 2026-10-02:

Playback is independent of the VPS. Browsers stream stems from CloudFront
with signed URLs (`media_ttl_seconds` is 24 h); the API serves the
manifest and takes telemetry. Five, ten or fifty simultaneous players are
well within CDN and VPS capacity. Egress is ~60-80 MB per song played
(working estimate): 50 people x 10 songs is ~35 GB, about $3.

Ingestion is serial. One orchestrator
(`library/src/shizzle_server/orchestrator/loop.py`, `process_job`) claims
a job and walks it through upload, RunPod separation, verification and
publication before claiming the next; RunPod `workers_max` caps GPU
parallelism and defaults to 0 until set. Five songs submitted at once
form a queue at roughly 5-10 minutes each; there is no queue-position
indicator beyond the pipeline dashboard. Lease fencing from PR #42 allows
several orchestrator workers; raising `workers_max` and running N workers
is the fix. VPS ffmpeg publication (4 vCPU) is the next ceiling, at about
3-4 parallel songs.

Single points of failure:

- One VPS (api, orchestrator, Postgres, Caddy; 4 vCPU, 16 GB, 193 GB
  disk, 31 GB used).
- One Postgres with no documented off-box backup (unknown; do not claim
  either way).
- One S3 bucket and one CloudFront distribution.
- One RunPod endpoint.
- Static long-lived AWS keys in the VPS `.env`; rotation is manual.
- The human-approved deploy gate.
- Tailscale for admin access.

Access: the passcode gate is intentionally open (owner decision
2026-09-03; any passcode is accepted); there are no accounts; device
tokens last 7 days; anyone with the URL can upload (RunPod cost) and use
whatever the UI exposes. Open issues #48 (per-source lock when two people
submit the same song) and #49 (drop-box hardening).

The remote mixer has no pairing (verified 2026-10-02 in
`library/src/shizzle_server/api/remote.py`). The relay keeps one room for
the whole deployment and fans every frame out to every other connected
client; the `/remote` page shows "the correct song" only because one
browser is playing. With two players, every remote flips between their
state snapshots and a fader move lands on both; anyone who opens
`/remote` controls whoever is playing. The fix is a room code: the
playing browser creates a room and shows a short code or QR
(`/remote?room=…`), the remote page asks for it, and the relay fans out
per room. It is a small change in `RemoteHub` plus two pages, and it is
the first thing a second household would notice.

At 5 friends, the shared remote room and the ingestion queue are the
first problems. At 10, hours of queue, duplicate submissions (#48) and
RunPod cold starts. At 50, access control and cost, not capacity.

Before sharing (working default): room codes for the remote, close #48,
add a queue-position indicator, decide who may add and remove songs, run
several orchestrator workers, set `workers_max`.

## Documentation policy (later, not this change)

The owner, 2026-10-02:

> "at some point, I would like to really scrub all the documentation and
> describe things as they are. Everything else that's not in the active
> build should be relegated to an archive file, their history file or
> something. In the regular documentation, we shouldn't have any legacy
> stuff."

Wanted future task. Sketch: the regular docs describe the active build in
the present tense; dated reviews, spike results, retired-tool notes and
acceptance records move into one archive with an index. Docs-only merges
skip the production deploy.

## Open repository items

- `deploy-gate` classifies `HEAD^..HEAD`, not the full push range.
- Greptile residue outside the docs classifier (`.coderabbit.yaml`,
  `.greptile/`, `ops/render_pr_review_goal.py` and its test,
  `templates/.../review-policy.json`, template bootstrap probes) waits
  for a deployable change.
- A historical passcode value sits in
  `evidence/spikes/RESULTS-frontend-deploy.md`.
- Nine issues remain open from the 2026-09-04 review (#23, #24, #27,
  #29, #30, #31, #35, #36, #37); see [REVIEW.md](REVIEW.md).
