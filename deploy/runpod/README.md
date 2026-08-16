# deploy/runpod — GPU worker platform configuration

State of the RunPod side of the system. The worker code is `stemsplit/`;
this directory records how it runs in the cloud.

## Current facts

- Endpoint: `tevdw8022hs8hn`.
- Worker image: `ghcr.io/mdc159/shizzle-worker:v2` (legacy package, linked
  to the retired spike repo `mdc159/shizzle-worker`; still what the endpoint
  pulls). New builds publish immutable `sha-<commit>` tags to
  `ghcr.io/mdc159/shizzle/worker` via `.github/workflows/worker-image.yml`,
  building from `stemsplit/` — repoint the endpoint there at cutover.
- Pool: last observed one GPU per worker, allowed GPUs RTX A5000 / L4 /
  RTX 3090 / RTX A4000, and `workersMax=0` — the pool is deliberately
  parked; no GPU can currently be allocated.
- A complete separation with real S3 input/output and both integrity gates
  was previously proven on an RTX 4070.

## Platform limits policy

Every limit here is set as measured local baseline plus margin, then
trimmed from telemetry — never guessed:

- **Container disk**: size from the measured working set (source + six
  float32 WAVs + model), oversized generously; disk is cheap.
- **Execution timeout**: derived from measured wall-clock per track length
  on the local proving-ground GPU (the most constrained card involved),
  times 2–3. This is a loose backstop only — the orchestrator's per-job
  watchdog (heartbeat age per phase) is the working monitor.
- **Segment size**: whatever the 8 GB local card proves ships everywhere;
  the pool's 16–24 GB cards inherit at least 2x margin.
- **GPU pool**: start with the 24 GB cards, widen once boring.

## Publishing a new image

Images are immutable and published only through the GitHub Actions
workflow in this repo — never pushed by hand. The RunPod template is then
pointed at the new tag. `handoff.json` records which image ran each
separation.
