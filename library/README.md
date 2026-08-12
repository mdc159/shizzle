# Shizzle server

FastAPI control plane for authentication, library access, ingestion, durable job
orchestration, publication, playback telemetry, and production health.

## Current boundary

- Browser delivery and playback are complete for the accepted 27-track library.
- RunPod hands the VPS the exact `lossless-stem-v1` package: six stereo,
  44.1 kHz IEEE float32 WAV stems with identical timelines plus `handoff.json`.
- The active server work is upstream ingestion: submit a URL or upload,
  dispatch a cloud GPU separation job, reconcile completion, receive that
  package, and pass it to the finished delivery pipeline.

The complete interface is
[`../interfaces/lossless-stem-v1/spec.md`](../interfaces/lossless-stem-v1/spec.md).

The server controls work and grants media access. It does not relay the normal
seven-stream playback path; signed media flows from CloudFront to the browser.

## Main areas

- `src/shizzle_server/db/` — SQLAlchemy models and repository layer for jobs,
  events, tracks, active generations, playback sessions, and telemetry.
- `src/shizzle_server/orchestrator/` — durable lease loop, retry scheduling,
  idempotent stage handling, and restart recovery.
- `src/shizzle_server/publish.py` — staged-object verification and immutable
  generation publication with the manifest written last.
- `src/shizzle_server/media_audit.py` and `delivery_profile.py` — executable
  `shizzle-browser-v1` checks.
- `alembic/` — production Postgres migrations.

## Production behavior

1. Persist an ingestion job.
2. Acquire and validate a source in cloud infrastructure.
3. Dispatch cloud GPU separation and persist its provider job id.
4. Reconcile by callback and polling until a terminal result exists.
5. Receive the complete `lossless-stem-v1` package.
6. Run the finished delivery pipeline.
7. Publish an immutable generation and activate its database pointer.

Steps 2–5 are the active implementation work. Steps 6–7 and browser playback
are established.

## Validation

```text
uv run --directory library ruff check .
uv run --directory library pytest -q
uv run --directory library mypy .
```

The Postgres contract suite covers restart recovery, expired leases, duplicate
completion, retry timing, concurrent orchestrators, structured failures, and
schema constraints.
