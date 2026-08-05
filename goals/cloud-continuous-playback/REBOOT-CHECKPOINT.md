# Current checkpoint: Shizzle

Date: 2026-08-05

## Finished

- The cloud delivery and browser playback portion is complete.
- Production contains exactly 27 accepted tracks.
- All 27 active generations pass `shizzle-browser-v1`.
- The established path begins with six clean aligned lossless stems and ends
  with immutable cloud publication and reliable browser playback.
- Existing accepted tracks are not scheduled for reprocessing or another
  development validation campaign.
- The runtime is entirely cloud-hosted and uses one standards-based browser
  media path.

## Active work

The next portion begins before the finished boundary:

```text
URL or upload → cloud acquisition → cloud GPU split → clean lossless stems
```

Immediate sequence:

1. Bake `htdemucs_6s` weights into the worker image.
2. Re-enable bounded RunPod capacity.
3. Prove one golden job reaches `COMPLETED` with verified S3 outputs.
4. Wire the production orchestrator to RunPod submission, reconciliation, and
   completion handling.
5. Make URL acquisition cloud-reliable while preserving direct upload.
6. Hand passing lossless stems to the unchanged finished delivery pipeline.

## Operational facts

- VPS: `72.60.173.171`, production stack `/opt/shizzle/prod`, compose project
  `shizzle`.
- Public application: `https://shizzle.systems`.
- Media: private S3 through signed CloudFront delivery.
- Current RunPod endpoint: `tevdw8022hs8hn`; it was last observed with
  `workersMax=0` and therefore cannot allocate a worker until re-enabled.
- Worker weights are not currently baked into `worker/Dockerfile`.
- Publish worker images through the `mdc159/shizzle-worker` GitHub Actions
  workflow.
- Clear any ambient `AWS_ENDPOINT_URL` before real AWS commands.

## Troubleshooting evidence

The evidence directory retains the development diagnostics, including one
interrupted 23-track natural playlist. Those files support targeted playback
troubleshooting; they are not open requirements and should not be resumed as a
default campaign.

## Worktree

The worktree contains extensive uncommitted playback, telemetry, audit, worker,
and documentation changes. Do not discard unrelated changes and do not commit
without Mike's explicit instruction.
