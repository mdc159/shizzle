# Current handoff

Use the [README](../README.md) for the application overview and
[Setup](SETUP.md) to bring up the API, player, or cloud pipeline.

The code implements upload, S3 source transfer, RunPod submission and polling,
lossless verification, browser derivation, immutable publication, playback,
remote mixing, and the pipeline dashboard. Since #63 a pasted YouTube link
is downloaded and separated by a worker node (the Mac mini first), with
RunPod as the separation fallback; the node agent lives under `node/`. With
`SHIZZLE_NODE_ROUTING=off` a URL job still fails `YTDLP_BLOCKED`, as before.

Not yet done after #63: the first live URL run through shizzle.systems with
the Mac agent online, the node's IAM user, `SHIZZLE_NODE_TOKENS` in the
production `.env`, and the Mac's launchd service. See the status section of
[Worker nodes](WORKER-NODES.md).

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
- [Worker nodes](WORKER-NODES.md): the Mac mini and Dell 9530 as
  first-choice download and separation executors, RunPod as fallback; the
  as-built runtime, install steps (`node/README.md`) and what is still open.
- [Lossless handoff](../interfaces/lossless-stem-v1/spec.md) and
  [browser delivery](../interfaces/shizzle-browser-v1/spec.md): format contracts.

Experiments under `evidence/` are retained for reproducibility. Their dated
results describe those runs, not current deployment status.
