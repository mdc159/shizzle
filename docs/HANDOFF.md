# Current handoff

Use the [README](../README.md) for the application overview and the system
diagrams, and [Setup](SETUP.md) to bring up the API, player, or cloud
pipeline.

The code implements upload and YouTube-link submission, worker-node download
and separation with RunPod as the separation fallback, S3 source transfer,
lossless verification, browser derivation, immutable publication, playback,
remote mixing, and the pipeline dashboard.

## Production state (2026-11-05)

- <https://shizzle.systems> runs master `b6cf712` (#63), routing `prefer`.
- Two worker nodes are registered and online: `mac-mini` (native, Apple
  `mps`) and `dell-9530` (container in WSL, CUDA). Each has its own node
  token in the production `.env` and its own scoped IAM user. Setup, update
  and checks: [`node/README.md`](../node/README.md).
- The first YouTube link through production published on 2026-11-05 (job
  `94a95b30`): download and separation on `mac-mini`, no RunPod involvement.
- Uploaded files also separate on a node when one is free; RunPod takes a
  separation only when no node does within two minutes.

Endpoint pool size, deployed digest, database contents, node availability
and prior acceptance runs are live facts, not source-code guarantees.
Confirm them using [Deployment and rollback](AUTOMATION.md), the
[RunPod runbook](../deploy/runpod/README.md), and `/dashboard`.

## Open

- Record the first live URL run and a run with both node agents stopped
  (the RunPod fallback) under `evidence/`.
- The Mac mini's disk is about 99% full; a long video can exhaust it during
  a job.
- Issue #29: Download Multi-Track cannot succeed for cloud uploads.
- PR #50 (authenticated finished-media import) stays parked; nodes use the
  lossless path instead.
- Worker-node follow-ups (per-assignment STS credentials, more than one job
  per node, heartbeat disk reporting) are listed in
  [Worker nodes](WORKER-NODES.md#open-items).

The [current review](REVIEW.md) records the inspected revision, tests, live
browser observations and GitHub issues. Review those issues before deciding
on runtime changes.

## Map

- [Architecture](architecture.md): component and job/remote swim lanes.
- [Worker nodes](WORKER-NODES.md): the nodes, routing, node API and
  credentials; install in [`node/README.md`](../node/README.md).
- [Invariants](INVARIANTS.md): required contracts and guards.
- [Testing](TESTING.md): automated, stem optimization, browser stress and listening procedures.
- [Playback troubleshooting](playback-troubleshooting.md): diagnosis and evidence.
- [Contributing completed media](contributing-completed-media.md): the S3 drop-box
  for finished `shizzle-browser-v1` packages and its VPS ingest (issue #45).
- [Lossless handoff](../interfaces/lossless-stem-v1/spec.md) and
  [browser delivery](../interfaces/shizzle-browser-v1/spec.md): format contracts.
- [Archive](archive/): planning records kept for their reasoning, not
  current behavior.

Experiments under `evidence/` are retained for reproducibility. Their dated
results describe those runs, not current deployment status.
