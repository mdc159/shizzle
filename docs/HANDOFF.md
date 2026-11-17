# Current handoff

Use the [README](../README.md) for the application overview and
[Setup](SETUP.md) to bring up the API, player, or cloud pipeline.

The code implements upload, S3 source transfer, worker-node URL download,
node/RunPod routing, RunPod submission and polling, lossless verification,
browser derivation, immutable publication, playback, remote mixing, the
pipeline dashboard, and the administrator page (`/admin`: job tracing with
evidence checks, node use switches, runtime routing and RunPod-limit
control through the Tailscale-only proxy). URL download needs a worker node:
with node routing off, URL jobs fail with `YTDLP_BLOCKED`.

The production URL is <https://shizzle.systems>. Endpoint pool size,
deployed digest, database contents, and prior acceptance runs are live facts,
not source-code guarantees. Confirm them using
[Deployment and rollback](AUTOMATION.md) and the
[RunPod runbook](../deploy/runpod/README.md).

The [current review](REVIEW.md) records the inspected revision, tests, live
browser observations and GitHub issues. Review those issues before deciding
on runtime changes.

- [Architecture](architecture.md): component and job/remote swim lanes.
- [Invariants](INVARIANTS.md): required contracts and guards.
- [Testing](TESTING.md): automated, stem optimization, browser stress and listening procedures.
- [Playback troubleshooting](playback-troubleshooting.md): diagnosis and evidence.
- [Contributing completed media](contributing-completed-media.md): the S3 drop-box
  for finished `shizzle-browser-v1` packages and its VPS ingest (issue #45).
- [Worker nodes](WORKER-NODES.md): plan for the Mac mini and Dell 9530 as
  first-choice download and separation executors, RunPod as fallback.
- [Setup](SETUP.md#administrator-page-local-development): the administrator
  page's access model (Tailscale proxy secret, `SHIZZLE_ADMIN_OPEN` for local
  development only).
- [Lossless handoff](../interfaces/lossless-stem-v1/spec.md) and
  [browser delivery](../interfaces/shizzle-browser-v1/spec.md): format contracts.

Experiments under `evidence/` are retained for reproducibility. Their dated
results describe those runs, not current deployment status.
