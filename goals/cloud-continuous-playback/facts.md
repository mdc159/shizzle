# Accepted playback facts

## Finished scope

- The delivery pipeline begins with six clean aligned lossless stems.
- The production library contains exactly 27 accepted tracks.
- All 27 active generations pass `shizzle-browser-v1`.
- All required processing, verification, publication, storage, APIs, monitoring,
  media delivery, and playback are cloud-hosted.
- Browsers consume one standards-based contract defined by web capabilities.

## Measured facts

- All 189 active media objects were hashed, probed, keyframe-inspected, and
  completely decoded on cloud infrastructure.
- All 162 active stems are stereo AAC-LC and aligned within their tracks.
- The Pot's active generation uses six 44.1 kHz stereo AAC-LC stems and a
  bounded audio-less H.264 video.
- The final 27-track production Chromium stress and bounded-fault matrices pass.
- The Pot passes repeated natural playback, randomized seeking, live mixing,
  recovery, natural end, and replay.
- Direct playback health is exposed through
  `window.__shizzlePlaybackHealth.getMetrics()` and measures individual media
  clocks plus rendered audio output.

## Decisions

- Preserve lossless upstream material as FLAC where practical; float WAV is
  allowed as temporary worker scratch.
- New browser delivery stems target stereo M4A/AAC-LC at 44.1 kHz and 256 kb/s
  and may not fall below 192 kb/s.
- Preserve passing aligned AAC instead of transcoding it merely to change the
  nominal bitrate or sample rate.
- Use one common measured gain for all six stems; never normalize them
  independently.
- Publish immutable generations, write the manifest last, and activate the
  database pointer only after the candidate passes.
- Deliver signed media directly through CloudFront. The VPS is the control
  plane, not a continuous seven-stream relay.
- Determine health from direct clocks, buffers, decoder state, Web Audio state,
  per-stem PCM, master PCM, limiter state, and recovery—not UI appearance.

## Forward boundary

- A future track with clean lossless stems runs through the finished pipeline
  once and joins the library if its routine checks pass.
- The accepted 27-track baseline is complete and is not scheduled for another
  validation campaign.
- The next work is cloud source acquisition and dependable cloud GPU separation
  into clean lossless stems.
- An observed future production issue may reopen only the affected behavior.
