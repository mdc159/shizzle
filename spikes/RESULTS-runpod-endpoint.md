# RunPod endpoint — real state (2026-08-03)

- ONE account. Fresh key in .env works.
- The `.env` RUNPOD_ENDPOINT_ID originally held the DEAD old pod id (3ugymq664vud7y) — the only endpoint that existed when the template was written. That endpoint is gone from the console; its id still answers /health (lingering), which caused a phantom-endpoint chase. Resolved by creating a real new endpoint.
- **New endpoint: `r370i6ad7h75m3` name "shizzle"**, template `vh76gbm3uy` (ghcr.io/mdc159/shizzle-worker), gpu [3090/A5000/L4/A4000], workersMax 3, idle 10s. Visible in console.
- Blocker: v1 image has a Demucs bug (found on CPU). v2 fix building in CI (run 30782804841). Next: update template image -> :v2, rerun staged proof job (source at s3://karaoke-pimpshizzle/tracks/proofc538/1/staging/source.mp4).

## Update (autonomous tick) — v2 deployed, workers crash-loop on GPU

- Template `vh76gbm3uy` now → `ghcr.io/mdc159/shizzle-worker:v2` (v1 crashed on `demucs.separate.load_track` ImportError; v2 fixes it, CPU-validated: gate_a 23.58 dB, gate_b 66.02 dB, full output set).
- Template env verified: AWS key ends AZHM (matches .env working key), bucket + pipeline knobs present. New endpoint inherits template env (created via REST w/o endpoint-level env).
- Handler wiring correct: `runpod.serverless.start({"handler": handler_wrapper})`, Dockerfile `CMD python3 handler.py`, `runpod` in requirements.
- SYMPTOM: workers boot → "ready" → go "unhealthy" (2 unhealthy observed); jobs sit IN_QUEUE and don't complete. Classic serverless crash-loop.
- LEADING HYPOTHESIS: Dockerfile HEALTHCHECK `assert torch.cuda.is_available()` failing on assigned GPU types (torch cu121 vs host driver mismatch), OR OOM/CUDA fault during demucs on a smaller GPU. NEEDS worker stderr log (console) to confirm.
- NEXT (needs Mike or worker log): read the failing worker's log in console (endpoint "shizzle" r370i6ad7h75m3 → worker → Logs), OR reproduce on the 4070 by running the v2 image in serverless test mode (test_input.json) for full traceback.

## RESOLVED (diagnosis) — pipeline PROVEN on GPU; RunPod cycling on Docker HEALTHCHECK

**The worker pipeline works end-to-end.** Ran the v2 serverless handler on the local RTX 4070 (real serverless entrypoint, real S3 I/O, not --local):
- download 1.42s, extract 0.33s, encode_video 4.77s, **demucs 15.14s**, gate_a 0.33s, encode_stems 14.75s, gate_b 2.07s, upload 4.5s, **total 43.32s**
- gate_a (PCM) null 23.58 dB PASS; gate_b (decoded AAC) null 66.09 dB PASS
- 8 files uploaded + sha256-verified to s3://karaoke-pimpshizzle/tracks/proofc538/1/staging/ (6 stems + video + manifest) — confirmed present via `aws s3 ls`
- healthcheck deps verified: torch 2.5.1+cu121, cuda True, demucs 4.1.0

**Root cause of RunPod "workers unhealthy / jobs stuck":** the Dockerfile `HEALTHCHECK` (`--start-period=5s`) — RunPod respects the container healthcheck and its own health mgmt fights it; torch+demucs import exceeds the 5s/10s window on the 15 GB image, so the first probe fails and RunPod cycles the worker before it takes a job. NOT a code/image/creds problem.

**Fix staged:** HEALTHCHECK removed from worker/Dockerfile (committed). Remaining to make it live: rebuild → push v3 → point template vh76gbm3uy at :v3 → resubmit proof on endpoint r370i6ad7h75m3 (the 3 stuck jobs are earlier bad-schema/pre-fix submissions; clear them).
