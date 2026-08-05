# Completed playback work and next implementation

## Status

The cloud delivery and browser playback portion is complete.

The accepted baseline is 27 production tracks. They have audited immutable
generations, browser-ready media, direct cloud delivery, synchronized six-stem
mixing, seeking, recovery, replay, direct health sensing, and retained evidence.
No additional playback campaign is planned.

## Simple workflow

```text
URL or upload
    ↓
Acquire source in the cloud                 NEXT WORK
    ↓
Separate six clean lossless stems           NEXT WORK
    ↓
Run finished delivery pipeline              COMPLETE
    ↓
Publish accepted track to the library       COMPLETE
    ↓
Play and remix from a conforming browser    COMPLETE
```

## Finished portion

Starting with six aligned lossless stems and source video:

1. Encode the six browser stems using `shizzle-browser-v1`.
2. Copy or derive the browser-safe audio-less video.
3. Run the fixed artifact, alignment, decode, checksum, and audio-safety checks.
4. Publish media to a new immutable generation with the manifest last.
5. Activate the database pointer only after the candidate passes.
6. Serve signed media directly from CloudFront.
7. Play, seek, mix, recover, end, and replay using the established browser
   engine and direct watchdog.

That is the normal path for every future track. It is not a new design exercise
or a reason to rerun the existing library. If a future track fails a routine
check, that candidate stays out of the library and the specific failure is
fixed. If production later exposes a real playback defect, address that defect
then.

## Current accepted library

- 27 active tracks.
- 27/27 active immutable generations pass `shizzle-browser-v1`.
- 189 active media objects completely audited and decoded.
- 162 AAC stems aligned within their tracks.
- 27/27 final production Chromium stress runs pass.
- 27/27 bounded-fault runs pass.
- The Pot repeated natural playback and replay pass.
- Direct browser health and first-party telemetry are live.
- Entirely cloud-hosted runtime with one standards-based browser media path.

## Next work: source to lossless stems

### 1. Make cloud GPU execution dependable

- Bake `htdemucs_6s` weights into the worker image.
- Re-enable a bounded RunPod worker pool.
- Submit the golden fixture.
- Require a real `COMPLETED` job with verified S3 outputs.
- If it fails, use the worker's actual cloud logs rather than changing the
  finished delivery path.

### 2. Connect production ingestion

- Accept a URL or direct upload.
- Validate the source before dispatch.
- Persist job identity, attempts, leases, retries, and the RunPod job id.
- Reconcile completion by callback and polling.
- Keep retries idempotent.

### 3. Produce clean lossless stems

- Extract source audio without clipping or unintended gain changes.
- Separate vocals, drums, bass, guitar, piano, and shizzle/other.
- Preserve aligned lossless outputs in cloud storage.
- Verify duration, finiteness, sample alignment, reconstruction, and role
  completeness.
- Pass only clean candidates into the finished delivery pipeline.

### 4. Make URL acquisition cloud-reliable

- Keep direct upload available regardless of URL-provider behavior.
- Solve URL acquisition entirely in cloud infrastructure.
- Keep source acquisition entirely within cloud infrastructure.

## Completion of the next portion

The upstream ingestion portion is complete when a newly submitted source can,
without local infrastructure, reach six verified lossless stems and then pass
unchanged through the finished delivery pipeline into the production library.

## References

- `encoding-profile.md` — exact finished format and artifact contract.
- `evidence.md` — measurements and failures that established the contract.
- `acceptance-matrix.md` — concise record of the completed playback result.
- `browser-conformance.md` — capability-based browser contract.
- `docs/HANDOFF.md` — live infrastructure and immediate next actions.
