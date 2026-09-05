# Current handoff

Use the [README](../README.md) for the application overview and
[Setup](SETUP.md) to bring up the API, player, or cloud pipeline.

The code implements upload, S3 source transfer, RunPod submission and polling,
lossless verification, browser derivation, immutable publication, playback,
remote mixing, and the pipeline dashboard. URL acquisition remains a stub
that fails with `YTDLP_BLOCKED`.

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
- [Lossless handoff](../interfaces/lossless-stem-v1/spec.md) and
  [browser delivery](../interfaces/shizzle-browser-v1/spec.md): format contracts.

Experiments under `evidence/` are retained for reproducibility. Their dated
results describe those runs, not current deployment status.
