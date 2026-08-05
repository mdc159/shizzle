# Shizzle browser delivery profile

Status: executable library-wide contract. All 27 active immutable generations passed complete artifact gates as of 2026-08-05; 25 are generation 2, The Pot is generation 3, and Into The Void is generation 3 after a measured common-gain correction.

Profile identifier: `shizzle-browser-v1`

This record separates three things that must not be conflated:

1. Lossless processing and archive artifacts.
2. Browser delivery artifacts.
3. Playback and listening acceptance.

Passing an FFmpeg probe is necessary, but it is not proof that a song sounds good or plays continuously in a real browser.

## Decisions

| Concern | Decision | Reason |
|---|---|---|
| Processing/archive stems | Preserve lossless float WAV or FLAC | Separation, reconstruction, gain analysis, and future re-encoding need lossless data. FLAC is preferred for retained storage when tool compatibility permits; WAV is acceptable as transient worker scratch. |
| Browser stems | M4A containing AAC-LC, stereo | This is the practical cross-browser delivery format for six simultaneous seekable streams. It is much smaller than WAV/FLAC and is supported by the target browsers. |
| Sample rate | Use one rate for all stems in a track; `44.1 kHz` is the new-encode target; preserve passing aligned `48 kHz` AAC | The full audit found 156 aligned legacy stems at 48 kHz and six Pot stems at 44.1 kHz. Resampling passing lossy AAC would add loss without improving playback. Never mix rates within a track. |
| New stem bitrate | `256 kb/s` target per stereo stem; `192 kb/s` hard floor | Six 256 kb/s streams are a reasonable quality/bandwidth tradeoff. Lower rates require a listening-backed profile revision. |
| Existing lossy stems | Preserve passing AAC bit-for-bit | Transcoding an existing 192-295 kb/s AAC file to 256 kb/s does not restore information; it adds generational loss. Re-encode only from a lossless source or when a file fails a delivery gate. |
| Audio fast start | Require `moov` before `mdat` | The browser can begin metadata parsing and range-based playback without downloading the complete object. |
| New/repaired video | Audio-less 720p MP4, H.264 Main Level 3.1, `yuv420p`, 30 fps, CRF 20, `1200k` max rate | This removes an unused competing audio track, bounds aggregate delivery bandwidth, and targets broad hardware decoding. CRF controls quality while max-rate bounds pathological sources. |
| Passing existing video | Preserve H.264 Main/High, `yuv420p`, and compatible source frame rate when every other gate and the 2.5 Mb/s track budget pass | The audit found 26 High-profile videos across six common frame rates. Re-encoding a passing lossy video creates generational loss and wasted work; only 14 over-budget videos require derivation. |
| Video seeking | Closed two-second GOP: `-g 60 -keyint_min 60 -sc_threshold 0` at 30 fps | Frequent predictable keyframes bound random-seek decode work and recovery time. |
| Video timestamps | Start at zero and run on the same declared timeline as the stems | A clean common timeline makes drift measurement and recovery deterministic. |
| Video fast start | Require `+faststart` | The repaired Pot video demonstrated that placing metadata first and using a bounded GOP materially improves browser seek behavior. |
| Mix gain | Apply only one common pre-delivery gain to all stems; store it as `default_gain_db` | Independent stem normalization changes the musical balance and invalidates reconstruction comparisons. |
| Output protection | Master headroom, true-peak-aware limiter, and post-limiter meter | Six stems and user gain changes can overload the master bus even when every individual file is valid. |
| Publication | Immutable generation; manifest written last; activate only after verification | A partial or bad batch never replaces the last-known-good generation. Rollback is a database pointer change, not a media rewrite. |
| Browser delivery path | File-scoped, 24-hour CloudFront signed URLs returned only by the authenticated manifest; private S3 remains behind OAC | A full-song A/B proved that relaying seven Range streams through Caddy/VPS introduced recurring low-water video stalls. Direct edge delivery removed the relay while retaining object-level expiry, authenticated grant, Range, and CORS. Same-origin `/cdn` remains the tested rollback path. |
| Master-video loading | After six-stem readiness, fetch the current audio-less video once from its signed CloudFront URL, enforce a 128 MiB ceiling, and play from a revocable browser Blob; release it on track change | The first direct-edge isolated War Pigs run passed, but the retained five-track playlist later recorded two 15-second master-video starvation windows while every stem remained healthy. The 66.49 MiB Blob regression reached natural end with zero incidents, then passed 20 random seeks with 316 ms maximum settlement. The library maximum is 80.3 MiB, so one bounded video is a deliberate reliability trade rather than unbounded preloading. |

## Standard cloud object layout

Each delivery generation is a self-contained immutable directory in the private bucket:

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

The database stores the active integer generation. The manifest declares the common timeline, delivery-profile id/details, the six required roles, common default gain, source generation, per-object copy/derive action and reason, source/output hashes, byte counts, and the audit hash. Media objects are uploaded and verified first; `manifest.json` is published last; the database pointer changes only after the complete candidate passes. The authenticated API rewrites those relative paths to expiring, file-scoped CloudFront URLs. The six AAC elements issue Range requests directly to the edge. After stem readiness, the browser fetches the one bounded audio-less video directly from the edge and gives the video element a revocable Blob URL, eliminating network starvation from the master clock and making scrubbing deterministic. Query credentials are never copied into telemetry or durable browser evidence. The runtime and media path are entirely cloud-hosted.

Lossless separation/processing material is upstream source material, not part of this seven-stream browser directory. Keeping FLAC or working WAV separate prevents a browser client from accidentally selecting a many-hundred-megabyte processing artifact and lets a future generation be re-derived without mutating the active delivery set.

## Full-library measurements that fixed the contract

The frozen 27-track audit downloaded, hashed, probed, keyframe-inspected, and completely decoded all 189 media objects on the VPS. All 162 stems were AAC-LC stereo; 156 were 48 kHz and six were 44.1 kHz. Every track's six stems had identical durations. The largest common AAC encoder tail beyond the declared timeline was 66 ms, which drove the 80 ms common-tail tolerance and the stricter 5 ms inter-stem duration-spread gate.

The 27 source videos comprised one H.264 Main and 26 H.264 High objects at 23.976, 24, 25, 29.97, 30, or 59.94 fps. All 189 objects fully decoded. Fourteen tracks exceeded the 2.5 Mb/s aggregate budget and therefore require bounded video re-derivation; 13 videos pass the compatibility/bandwidth gates and are copied bit-for-bit. These measurements changed the initial blanket-transcode proposal into a compatibility-preservation policy.

The 139 `audio-bitrate-low` findings are warnings on existing variable-rate AAC, not evidence that up-transcoding will improve it. A nominal bitrate is an encoder input target, not proof of retained musical information. Existing AAC is accepted by codec, alignment, decode, fast-start, reconstruction/listening, and browser behavior; the 192 kb/s floor is enforced for newly derived delivery stems.

The decoded six-stem analysis initially passed 26 of 27 active tracks before limiter action. Into The Void measured `-0.8 dBTP`, 0.2 dB above the `-1.0 dBTP` ceiling, without sample clipping. The tool recommended `-0.2 dB` additional common gain. Generation 3 applies exactly `-0.2 dB` to all six `default_gain_db` values, records the measurement and reason in manifest integrity, and remeasures at `-1.0 dBTP`. The final library result is 27 of 27 pre-limiter passes with zero clipped, NaN, or infinite samples. This is the evidence-driven use of common gain; no stem was normalized independently.

## The Pot generation 3: measured delivery

All six stems are AAC-LC, stereo, 44.1 kHz, start at zero, and report a duration of `378.648005 s`. Their M4A files have `moov` before `mdat`.

| Stem | Average bitrate | Bytes |
|---|---:|---:|
| vocals | 249,324 b/s | 11,867,684 |
| drums | 295,166 b/s | 14,037,630 |
| bass | 259,195 b/s | 12,334,946 |
| guitar | 259,913 b/s | 12,368,900 |
| piano | 250,891 b/s | 11,941,897 |
| shizzle | 237,408 b/s | 11,303,667 |

The active video is H.264 Main, `yuv420p`, 30 fps, start zero, duration `378.633333 s`, average bitrate `215,972 b/s`, and size `10,353,930` bytes. Its SHA-256 is `63603e74bc5a981b3d34a527952b1d8e359f02072dc9ba895a68c17772ea2c07`.

The complete seven-object delivery is approximately `84.2 MB` and `1.78 Mb/s` average. This is the measured pilot, not a requirement that every source be transcoded to exactly those bitrates.

## Reference derivation commands

Encode delivery stems only from aligned lossless inputs:

```text
ffmpeg -i aligned-stem.wav -map 0:a:0 -c:a aac -profile:a aac_low -b:a 256k -ar 44100 -ac 2 -movflags +faststart stem.m4a
```

The inputs to the six invocations must already have the same sample rate, start sample, and sample count. Do not use independent asynchronous resampling to make mismatched stems appear aligned.

Derive browser video:

```text
ffmpeg -fflags +genpts -i source-video -map 0:v:0 -an -vf "scale=w='min(1280,iw)':h='min(720,ih)':force_original_aspect_ratio=decrease:force_divisible_by=2,fps=30,format=yuv420p,setpts=PTS-STARTPTS" -c:v libx264 -preset medium -crf 20 -maxrate 1200k -bufsize 2400k -profile:v main -level:v 3.1 -g 60 -keyint_min 60 -sc_threshold 0 -movflags +faststart -video_track_timescale 90000 video.mp4
```

The exact command, FFmpeg version, input hashes, output hashes, measured stream metadata, and quality results must be written to each generation's provenance record. The versioned profile—not an undocumented command copied into two workers—is authoritative.

## Artifact acceptance gates

Every generation must pass all of these checks before activation:

- The manifest exposes exactly six required roles: vocals, drums, bass, guitar, piano, and shizzle. A legacy `other.m4a` source is mapped to the shizzle role and copied to the standard `stems/shizzle.m4a` path in a new generation.
- Every referenced object exists, has the expected content type and byte size, and matches its recorded SHA-256.
- FFmpeg fully decodes all six stems and the complete video with no decode error.
- All stems are AAC-LC in M4A, stereo, one common 44.1 or 48 kHz rate, zero-based within 20 ms, fast-start, mutually aligned within 5 ms, and no more than 80 ms beyond the declared timeline as a common encoder tail.
- A newly derived video is audio-less H.264 Main 3.1, `yuv420p`, 720p/30 fps, zero-based, fast-start, and has no keyframe gap greater than 2.05 seconds. A copied compatible video may retain Main/High and an enumerated source frame rate while meeting the same decode, timeline, fast-start, GOP, and bandwidth gates.
- Decoded video ends within 50 ms of the declared duration. A common AAC tail up to 80 ms is accepted only when all six stem durations remain within 5 ms of one another.
- Normal delivery should remain at or below 2.5 Mb/s total average bitrate. An exception requires a constrained-network browser pass and a recorded reason.
- Lossless reconstruction is measured before lossy encoding. Decoded-AAC reconstruction is measured separately. Neither result is misrepresented as a measurement of source-separation bleed.
- A single common gain is applied, if needed, and recorded as `default_gain_db`; relative stem gains are unchanged.
- Default playback has no sample clipping, a true-peak ceiling no higher than `-1 dBTP`, and no sustained heavy limiter action. The initial acceptance target is steady-state limiter reduction p99 at or below 1 dB and maximum at or below 3 dB, excluding labeled seek/start/fault recovery windows.
- Routine listening checks may cover the default mix, vocals-muted karaoke mix,
  soloed stems, transitions, and loud passages. They are useful for a new source
  or an observed quality concern; they are not an open full-library campaign.

## Why not browser WAV or FLAC?

For a track the length of The Pot, six 16-bit stereo WAV stems would be roughly 400 MB; float32 delivery would be roughly 800 MB. That multiplies startup, range, memory, and constrained-network risk without an audible benefit that has been demonstrated for this product. FLAC is valuable for archive retention, but it remains larger and less useful than AAC-LC for six concurrent browser streams. Lossless remains upstream; AAC-LC is the delivery derivative. This does not conflict with staging one audio-less video: the video is capped at 128 MiB, the frozen maximum is 80.3 MiB, exactly one Blob exists, and all six audio streams remain compressed and streaming.

## Completed evidence boundary

All 27 active generations pass the executable artifact profile and decoded
default-mix true-peak/sample-safety measurement. Clean-session direct readings
on The Pot, Orion, My Michelle, War Pigs, Hunger Strike, and Pearl Jam showed
approximately 0 dB settled limiter reduction with real post-limiter PCM; the
earlier 14 dB observation was contaminated by idle/prior-session compressor
state and is not reproduced when limiter reduction is sampled only while direct
PCM is expected.

This profile is the finished path from clean lossless stems to browser delivery.
Production Chromium provided the deterministic full-library implementation
evidence. The architecture remains one capability-based browser contract. No
additional full-library listening or browser-engine campaign remains open;
future work responds to actual new-track failures or observed production issues.
