# RunPod endpoint — real state (2026-08-03)

- ONE account. Fresh key in .env works.
- The `.env` RUNPOD_ENDPOINT_ID originally held the DEAD old pod id (3ugymq664vud7y) — the only endpoint that existed when the template was written. That endpoint is gone from the console; its id still answers /health (lingering), which caused a phantom-endpoint chase. Resolved by creating a real new endpoint.
- **New endpoint: `r370i6ad7h75m3` name "shizzle"**, template `vh76gbm3uy` (ghcr.io/mdc159/shizzle-worker), gpu [3090/A5000/L4/A4000], workersMax 3, idle 10s. Visible in console.
- Blocker: v1 image has a Demucs bug (found on CPU). v2 fix building in CI (run 30782804841). Next: update template image -> :v2, rerun staged proof job (source at s3://karaoke-pimpshizzle/tracks/proofc538/1/staging/source.mp4).
