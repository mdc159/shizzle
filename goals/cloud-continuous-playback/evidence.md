# Cloud playback evidence record

Evidence date: 2026-08-05

Production application: `https://shizzle.systems`

Acceptance track: The Pot, track id `f995371a-7d9b-5dbb-82d2-0000dc80aacf`, active media generation 3. Generation 2 remains its verified rollback predecessor.

## What failed and why

The legacy Pot video was approximately 3.6 MB at about 76 kb/s. Its MP4 metadata was at the end, keyframe gaps reached approximately seven seconds, and a complete FFmpeg decode reported `illegal short term buffer state detected`. Rapid production-browser seeks produced `MEDIA_ERR_DECODE` and the user-visible `Playback failed. Try again.` state.

This made the video artifact itself a demonstrated failure source. Playback state races and inadequate direct observation were separate contributing software risks.

## What changed

- The video was re-derived on the VPS as the profile recorded in `encoding-profile.md`: H.264 Main 3.1, 30 fps, two-second GOP, zero timestamps, no audio, CRF 20, and fast-start.
- The six AAC-LC stems passed artifact checks and were preserved bit-for-bit rather than needlessly transcoded.
- The repaired media was published as a new immutable generation; generation 1 remains the rollback.
- Play and pause commands now invalidate stale async work.
- The initiating click/touch starts the video, `AudioContext`, and stems in the same user gesture.
- Health now observes the video, each stem, the `AudioContext`, buffers, errors, and post-limiter PCM directly.
- The drift policy measures each stem instead of relying on an average that can hide one early and one late decoder.
- Recovery is bounded and every incident remains inspectable through `window.__shizzlePlaybackHealth.getMetrics()`.

## Production tests and results

| Test | Result |
|---|---|
| Complete natural play | `0.000 -> 378.633333`, `ended=true`; zero recovery, media error, fatal incident, or console error |
| Periodic full-pass samples | 0-3 ms inter-stem skew; maximum sampled stem/video offset 7 ms; rendered PCM present |
| Settled random seeking | 20 seeks; maximum recovery 2,062 ms, average 1,108 ms; maximum inter-stem skew 32 ms; maximum video offset 39 ms; zero fatal incidents |
| Rapid transport | 12 pause/play cycles; all stems and video playing at finish; zero new incidents |
| Mixer operations | Mute, unmute, solo, minimum gain, and reset affected the real gain nodes; healthy output remained present |
| Injected decoder fault | Bass media element forcibly paused; sensor recorded `stem-clock-stalled`; recovery succeeded and healthy PCM returned in 460 ms |
| Replay after ended | Healthy playback resumed at 3.010 s with 3 ms inter-stem skew and no fatal incident |
| Static checks | UI build and lint passed; server 101 tests passed with 9 skipped; Ruff and `git diff --check` passed |

The tests used controlled desktop Chrome against the public production site and cloud media boundary. The browser was only a test client; no client-side file, server, proxy, tunnel, callback, or helper participated in playback.

An earlier Windows Playwright WebKit environment exposed neither `AudioContext` nor `MediaElementAudioSourceNode`; that environment is not conformance evidence. WebKit must be rerun in a supported Playwright/WebKit or Safari environment against the same production contract.

## Current library baseline

The frozen pre-migration inventory contained 27 active tracks, 27 MP4 video objects, and 162 M4A stem objects. At that baseline, 26 tracks referenced generation 1 legacy imports whose manifests said `integrity.source=legacy-import` and `integrity.gates=not-run`; only The Pot used the verified generation 2 path.

The active library represented approximately:

- 8,344.041 seconds (2 h 19 m 4 s) of music.
- 3,496,387,949 bytes of media.
- 2,326,207,543 video bytes.
- 1,170,180,406 stem bytes.

The legacy audio total implies about 187 kb/s per stem on average; this is an inventory estimate, not an artifact-level pass. Every object still needs `ffprobe`, full decode, fast-start, timeline, checksum, and browser gates.

Large-video canaries include Metallica's Orion at 400,814,541 bytes, AC/DC's Shoot to Thrill at 243,018,174 bytes, and the live War Pigs video at 219,549,877 bytes. These are deliberately included early because they exercise buffering and seek behavior that The Pot does not.

## Complete artifact audit and active canaries

The VPS audit fully decoded all 189 frozen media objects with zero FFmpeg decode failures. It found 162 AAC-LC stereo stems: six at 44.1 kHz and 156 at 48 kHz. All six stem durations matched within each track. Twenty-six manifests required standard `stems/shizzle.m4a` paths, 14 tracks exceeded the 2.5 Mb/s aggregate delivery budget, and 139 low-average-bitrate AAC findings were classified as preservation warnings rather than reasons for lossy up-transcoding. The resulting migration plan copies 13 compatible videos and re-derives 14 over-budget videos.

Activated, fully audited immutable generations now include:

- The Pot generation 3: all eight generation objects verified; live rollback `3 -> 2 -> 3` succeeded without rewriting media.
- Orion generation 2: the 400.8 MB legacy video was re-derived to an 84,151,358-byte bounded delivery video; the complete candidate decoded and passed before activation. Twenty random seeks settled with 11 ms maximum inter-stem skew, 12 ms maximum stem/video offset, healthy PCM, and no fatal incident.
- My Michelle generation 2: compatible video and six stems copied into a standard generation; full candidate decode passed. An injected stopped bass stem was directly detected after a 248 ms watchdog interval and healthy playback/PCM returned in 515 ms.
- War Pigs generation 2: the 219.5 MB live video was re-derived on the VPS; the complete seven-object candidate decoded and passed before activation. Production playback showed six advancing stems, 3 ms maximum inter-stem separation, clean PCM, and no decoder error.

The first controlled rollout activated 16 tracks, then stopped before activation on Mother Love Bone - Stardog Champion because its 1460x1080 source produced an odd 1279-pixel derived width that libx264/yuv420p rejected. The shared scale filter was corrected to bound both axes and require `force_divisible_by=2`; a synthetic 1460x1080 regression test passed, the resumable rollout restarted at that track, and all remaining 11 tracks activated cleanly. A subsequent measured gain correction moved Into The Void to generation 3, so final database pointers are 25 tracks at generation 2, The Pot at generation 3, and Into The Void at generation 3.

Tracked production stress evidence on the current playback build includes:

| Track | Seek p95/max | Max inter-stem | Max stem/video | Stopped-stem recovery | Result |
|---|---:|---:|---:|---:|---|
| My Michelle gen 2 | 2,427 / 2,427 ms | 11 ms | 20 ms | 547 ms | pass |
| War Pigs gen 2 | 2,606 / 2,606 ms | 19 ms | 29 ms | 499 ms | pass |
| Orion gen 2 | 1,579 / 1,579 ms | 11 ms | 23 ms | 393 ms | pass |
| The Pot gen 3 | 2,528 / 2,528 ms | 30 ms | 40 ms | 457 ms | pass |

Each tracked run included 20 deterministic seeks, 12 rapid pause/play cycles, mute/solo/reset against real gain nodes, direct post-limiter PCM assertions, an injected stopped bass stem, and zero fatal incidents. Orion drove a `canplay` coordinated-resume fix for Chromium transitions that omit a second `playing` event. The Pot drove a stricter drift policy: ignore below 30 ms, nudge 30-50 ms, and hard-align at 50 ms; the earlier 50-100 ms nudge tier could not meet the three-second settled contract.

My Michelle's decoded six-stem default mix measured -12.8 LUFS integrated, 6.1 LU loudness range, -1.8 dBTP true peak, and -1.80 dBFS sample peak with the actual -3 dB master headroom. It contained no clipped, NaN, or infinite samples and passed before limiter action.

The first complete active-library quality run passed 26/27. Into The Void was not sample-clipped, but its decoded default mix reached -0.8 dBTP, 0.2 dB above the -1.0 dBTP ceiling. The measured recommendation was applied as one common -0.2 dB `default_gain_db` to all six stems in immutable generation 3. Its candidate objects and manifest passed the complete audit before activation, and its post-activation mix remeasured at -1.0 dBTP with no further gain required. The final complete run passes 27/27 before limiter action: maximum true peak -1.0 dBTP, zero clipped tracks, zero NaN samples, and zero infinite samples. Reports, candidate audit, activation event, and final measurements are stored under `evidence/vps/`.

Production random-seek testing also drove the direct video-buffer recovery policy. Hunger Strike failed while repeatedly restarting with only 0.30 seconds buffered. A 1.00-second reserve removed that loop but caused one clean Pearl Jam recovery to take 3.038 seconds, 38 ms over the unchanged gate. A provisional 0.50-second reserve passed targeted Hunger Strike, Pearl Jam, and War Pigs reruns, but broader evidence superseded that rule.

Mother Love Bone reported 1.55 seconds in `TimeRanges` while its decoder remained at `HAVE_METADATA`; downloaded bytes alone did not mean the target frame was decodable. Orion then showed that pausing the master to accumulate a reserve deprioritized the browser's Range fetch. Recovery now leaves the video play/fetch active, pauses all stems, and resumes the hard-aligned ensemble only on a real `playing` event or direct master-clock advancement. A redundant second `video.play()` was removed after its promise remained pending across `waiting` and blocked further recovery. Finally, the watchdog moved from 250 ms to 100 ms after Orion's clocks and PCM were already healthy but the status missed the three-second boundary, and the internal alignment boundary moved from 50 ms to 40 ms after a post-poll reading reached 51 ms. On the resulting policy, Orion passed with 2,739 ms maximum seek recovery, 11 ms inter-stem skew, 19 ms stem/video offset, 176 ms stopped-stem recovery, 24/24 recoveries, and zero fatal incidents. The external 3,000 ms and 50 ms gates were never relaxed.

Two browser-observability defects were found by canary testing and corrected before the broader rollout: intentional source teardown no longer emits a false `Empty src attribute` media failure, and recovery/incident counters now reset at track boundaries. Telemetry capacity was also increased from 120 to 600 events/minute with a 1,000-event ordered client queue and `Retry-After` backpressure after a measured seek storm generated roughly 200 events/minute.

The controlled library rollout is checkpointed by `scripts/migrate_and_audit_delivery_library.py`. It publishes one inactive immutable candidate, re-downloads and fully decodes all seven objects, activates with compare-and-swap only after a clean audit, and stops on the first failure.

## Final tracked desktop stress run

On final staged-video build `/assets/index-C3uc-Cjz.js`, `evidence/browser/library-27-stress-staged-video.json` passed the complete frozen library in one uninterrupted production Chrome session: 27/27 tracks, seed `1511464998`, 11.1 minutes, zero failed tracks, and zero fatal incidents. Every track directly proved a nonzero bounded video byte count and `blob:` master source, then executed 20 target-bound random seeks, 12 rapid pause/play cycles, real mixer mute/solo/reset, direct post-limiter PCM assertions, and an injected stopped stem. Cross-track transitions reused one page so Blob revocation/replacement, teardown, leaked media, timer/node cleanup, and inherited mixer state were exercised rather than hidden by fresh-browser runs. The report retains exact CloudFront object paths but redacts their signed queries.

Worst case across all 27 tracks:

| Metric | Result | Gate |
|---|---:|---:|
| Direct browser seek completion | 350 ms | <= 3,000 ms |
| Controller observation lag, recorded separately | 54 ms | informational; excluded from direct media recovery |
| Inter-stem skew after settlement | 11 ms | <= 50 ms |
| Stem/video offset after settlement | 16 ms | <= 50 ms |
| Injected stopped-stem recovery | 252 ms | <= 3,000 ms |
| Recovery attempts / successes | 541 / 541 | exact match |
| Fatal incidents | 0 | 0 |

All 27 final states were directly healthy with rendered PCM, maximum settled limiter reduction was 0.00014 dB, maximum final absolute stem/video skew was 28 ms, every video source was `blob:`, the largest single staged video was 80.25 MiB, and the complete transition run staged 928.8 MiB sequentially without retaining prior-track sources. The accumulated `peakReductionDb` field is not used as a steady-state quality claim because it intentionally retains seek/fault transients; decoded true-peak measurements and settled instantaneous readings are the applicable gates. Earlier `/cdn` and streaming-direct-edge reports remain as regression evidence but are superseded for the active transport by this run.

The final recovery architecture was driven by the failed intermediate runs. The video master receives a 200 ms Range-request head start, then paused stems prefetch the requested target concurrently. Recovery is bound to the requested target so stale pre-seek `playing` events cannot start it. After the target is proven by `playing` or actual clock advancement, a bounded barrier waits for decodable stems and coordinates starts; `waiting` during that barrier is handled inside the same recovery rather than superseding it. The 100 ms watchdog only marks health after video, every stem, post-limiter PCM, and the 40 ms internal synchronization margin all pass. Harness latency uses the browser's new per-seek `lastHealthyAtMs`; Playwright observation lag is recorded separately.

## Library-wide bounded-fault acceptance

The active-path production-Chromium fault run in `evidence/browser/library-27-faults-staged-video.json` passed all 27 tracks on build `/assets/index-C3uc-Cjz.js`. For each track, Playwright aborted one real signed CloudFront AAC byte-range request with a connection reset, froze and restored the Chromium page lifecycle, and sought under a declared 4,000,000 bit/s downstream, 1,000,000 bit/s upstream, 150 ms RTT cellular-4G emulation. The master remained the staged Blob, so the test specifically preserved the streamed-stem fault surface. The gate restored normal networking and required a directly captured healthy state so a connection-change transient could not be mistaken for completion.

| Measurement | Result |
|---|---:|
| Tracks passed | 27 / 27 |
| Real Range requests aborted | 27 / 27 |
| Maximum AAC Range-fault recovery | 719 ms |
| Maximum foreground restore | 43 ms |
| Maximum constrained-network seek recovery | 1,176 ms |
| Healthy after normal-network restoration | 27 / 27 |
| Fatal incidents | 0 |

An earlier `/cdn` iteration recovered every injected fault but sampled four tracks in a fresh transient `recovering` state immediately after removing network emulation. During the first direct-edge rerun, the final evidence read could also race immediately after a healthy predicate and capture a later transition. The harness now returns the exact healthy state that satisfied the direct predicate and requires another final healthy snapshot before marking a track passed. The complete 27-track matrix was rerun; the table above is the stricter result. This failure-driven change is evidence that final health is directly observed rather than inferred from the fault routine returning.

## Live first-party incident proof

An authenticated production query to `GET /api/playback/incidents?minutes=180&limit=200` returned the capped 200 most recent incidents across all 27 track ids and active generations 2 and 3. Every returned row named its track, generation, event, receive time, and included direct video plus six-stem clock/state detail; none was `recovery-failed` or a decoder error. The returned mix was 196 video-buffering observations, three deliberately induced stem-clock stalls, and one render-silence observation. This query proves that the server can identify a live production incident by track and generation without a screenshot, credential, media URL, or indirect audio sensor. Expected bounded incidents are retained even when recovery succeeds; final verdicts and recovery counters determine whether they are fatal.

## Natural-playback failure and direct-edge decision

The first uninterrupted playlist retained in `evidence/browser/library-27-natural-playlist.json` passed four tracks and then failed on War Pigs. At timeout, the video was still healthy at 431.944 of 462.933 seconds, all six stems were playing 13 ms behind the master, PCM was live, and six of six recoveries had technically succeeded. That was not accepted: recurring video Range starvation consumed about 90 seconds and prevented natural completion.

Caddy's contemporaneous logs showed repeated cancelled, reopened, open-ended video ranges through the VPS relay. Disabling QUIC removed the rapid HTTP/3 reopen pattern, but `war-pigs-natural-h2.json` still needed two recoveries of roughly 15 seconds and took 496.09 seconds. Direct private CloudFront was then measured before redesign: an authenticated 8 MiB Range returned 206 from a warm edge at 355.87 Mb/s; after the CORS policy was deployed, a cold end-to-end client Range returned 206 at 55.47 Mb/s with 1.17-second time-to-first-byte. The API was changed to return 24-hour, exact-object signed URLs from the authenticated manifest, preserving private S3/OAC while removing the VPS data relay.

`evidence/browser/war-pigs-natural-direct-edge.json` then completed the same media in 466.73 seconds, reached `ended` at 462.933, replayed at 0.123 seconds, and recorded zero buffering events, zero recoveries, zero media errors, and directly healthy output. `evidence/vps/cloudfront-before-direct-edge.json` plus the VPS source backup retain rollback state. Browser evidence now replaces every signed query with `?signed=redacted`; an explicit repository scan confirmed no `Policy`, `Signature`, or `Key-Pair-Id` query remains in durable browser reports.

That isolated result was necessary but not sufficient. The retained `evidence/browser/library-27-natural-playlist-direct-edge.json` passed four preceding songs, reached War Pigs at its exact 462.933-second end, but correctly failed because two master-video recovery windows took about 15.1 seconds each. At failure all six stems were aligned 34 ms behind the video with no decoder error and live PCM; the network-starved video master was the remaining defect. Direct CDN removed the VPS bottleneck but did not make Chromium's seven independent request priorities deterministic.

Build `/assets/index-C3uc-Cjz.js` now waits for six-stem readiness, downloads only the bounded audio-less master video from its signed CloudFront URL, rejects a declared or measured size above 128 MiB, creates a revocable Blob source, and enables Play only after video metadata is ready. The first concurrent-fetch attempt is retained for troubleshooting: starting the 66.5 MiB fetch alongside stem probes starved the existing 15-second stem load gate, so the final implementation deliberately sequences stems first. `evidence/browser/war-pigs-natural-buffered-video.json` then reached 462.933 seconds and replayed at 0.124 seconds with zero incidents or recoveries. `war-pigs-stress-buffered-video.json` directly records a `blob:` source and 69,720,945 staged bytes; 20 random seeks settled in at most 316 ms, inter-stem skew at most 6 ms, stem/video offset at most 11 ms, stopped-stem recovery in 165 ms, and final direct health.

The first full staged-video diagnostic playlist in `evidence/browser/library-27-natural-playlist-staged-video.json` ran for 2.4 hours and reached exact natural end plus healthy replay on 27/27 tracks in one browser session. It sequentially staged 928.8 MiB across 27 `blob:` sources with zero media errors. One track, Mother Love Bone - Stardog Champion, exposed two stricter defects: the first sensor treated a genuine silent intro as a dead graph and its replay did not become healthy until video time 7.325 seconds. That run is retained as diagnostic rather than promoted to final acceptance.

The replay path now explicitly rewinds the staged video and all six stems to zero before gesture-bound play calls. More importantly, build `/assets/index-BYm-VBo-.js` inserts a direct PCM analyser after every stem gain and compares actual source signal with post-limiter master output. A silent master is only a render fault when at least one audible stem is really producing PCM. `evidence/browser/stardog-input-pcm-replay-natural.json` proves the distinction: at replay, master measured -92.24 dBFS while every stem was below -105.60 dBFS, so the silent intro was healthy; replay settled in 313 ms at video time 0.118 seconds with zero incident or recovery. The 3,000 ms gate was not widened. The separate over-cap production test also proves a declared 128 MiB + 1 byte video fails closed before Play with zero staged bytes.

## Final implementation checks

The post-direct-edge rerun passed 146 server tests in 62.82 seconds; the latest
worker run passed 42/42 in 2.37 seconds. Ruff passed the server/source/scripts
and complete worker scopes. UI ESLint, TypeScript, and the Vite production build
passed on the deployed bounded-video build `/assets/index-C3uc-Cjz.js`.
`git diff --check` reported no whitespace error, only Windows line-ending
notices.

## Completed conclusion

- All 27 accepted tracks point to audited immutable generations with recoverable
  predecessors.
- All 27 pass the tracked production-Chromium seek, transport, mixer,
  direct-PCM, transition, and stopped-stem stress suite through the cloud
  runtime.
- All 27 pass the bounded network/media fault suite.
- All 27 decoded default mixes pass pre-limiter true-peak and sample-safety
  checks.
- The Pot and retained long-track canaries prove natural playback, end, and
  replay behavior.
- Direct browser instrumentation detects stopped or silent media paths rather
  than inferring health from UI state.

This evidence established the finished `shizzle-browser-v1` delivery and
playback protocol. The incomplete diagnostic playlist and earlier proposed
cross-engine/listening campaigns are retained as troubleshooting evidence, not
as open completion work. Future validation is limited to routine admission of a
new track or diagnosis of an actually observed production issue.
