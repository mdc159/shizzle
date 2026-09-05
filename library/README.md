# Shizzle server

FastAPI control plane for authentication, ingestion jobs, library access,
durable orchestration, immutable publication, playback telemetry, and the
remote mixer WebSocket relay. Production runs the API and orchestrator as
separate processes against Postgres. The API grants media access; browser
media normally travels directly from CloudFront.

## Implemented ingestion path

An authenticated video upload is saved under `DATA_DIR/<job-id>/source.mp4`
and hashed while being copied. In cloud mode, the orchestrator uploads it to
private S3 at `sources/<track-id>/source.mp4`, reserves a dispatch in Postgres,
and submits a RunPod job. Polling observes worker progress and completion;
worker-written S3 receipts and the handoff completion marker reconcile an
accepted dispatch whose response was lost. There is no completion callback
endpoint.

The worker returns the exact
[`lossless-stem-v1`](../interfaces/lossless-stem-v1/spec.md) package: six
stereo, 44.1 kHz float32 WAV stems with identical sample counts, followed by
`handoff.json`. Each dispatch uses its own `attempts/<hash>` prefix. The VPS
downloads and verifies the package, derives the browser media, verifies and
promotes staging objects with the manifest last, and commits the library row
and `ready` job state together. Successful cloud publication removes the
local job directory.

| Source / mode | Current behavior |
| --- | --- |
| Upload, `SHIZZLE_PIPELINE=cloud` | `pending → downloading → dispatched → verifying → publishing → ready`, subject to configuration and validation gates. |
| URL submission | Accepted as a job, then fails with `YTDLP_BLOCKED`; URL acquisition is not implemented. |
| Local reference pipeline | Runs local FFmpeg/Demucs through verification; publication of its `local/` location is refused by invariant C7. It is not an end-to-end library startup path. |
| Test pipeline | Deterministic fault-injection stages; requires `SHIZZLE_ALLOW_TEST_PIPELINE=1`, produces no playable media, and publication is refused. |

Cloud ingestion needs working RunPod configuration and available worker
capacity. The presence of implementation and passing unit tests does not
prove a deployment has those prerequisites.

## Code map

- `src/shizzle_server/api/` — passcode/device-token authentication, routes,
  CloudFront signing, manifest rewriting, and the deployment-wide remote relay.
- `src/shizzle_server/db/` — jobs, append-only events, tracks, generation
  activation, playback sessions/events, and orchestrator heartbeats.
- `src/shizzle_server/orchestrator/` — leases, stage transitions, retry/park
  behavior, RunPod polling/reconciliation, and cloud publication integration.
- `src/shizzle_server/publish/` — lossless intake, delivery policy, media/audio
  audits, immutable S3 promotion, and generation migration tooling.
- `alembic/` — Postgres schema migrations. The current linear chain ends at
  `0005_job_artist`; production migrations belong to the deploy transaction.
- `tests/` — isolated unit/media tests and the real-Postgres contract suite.

## Development setup

Use Python 3.11 or newer and `uv`. Run these commands from the repository root:

```powershell
uv sync --directory library --frozen
uv run --directory library uvicorn shizzle_server.main:app --host 127.0.0.1 --port 8001
```

This starts the default SQLite development API and embedded local orchestrator.
It is useful for API/UI development, not production ingestion. SQLite tables
are created automatically. FFmpeg and ffprobe must be on `PATH` for media
validation; the optional local reference splitter additionally needs Demucs,
PyTorch, and its model. `uv sync` does not install those splitter dependencies.
The player development server proxies `/api` to port 8001; see
[the player setup guide](../docs/SETUP.md#local-api-and-player).

Settings are read from process environment variables. `Settings` does not
automatically load a repository `.env` file; Compose supplies its configured
environment files. The default passcode is empty, which disables the API auth
gate. Configure real authentication before exposing a deployment.

## Production prerequisites

Use the maintained
[`production deployment runbook`](../docs/AUTOMATION.md) and
[`production Compose file`](../deploy/vps/compose.prod.yml). The API image is
`library/Dockerfile.api`; the orchestrator runs that same image without a GPU.

| Configuration | Purpose |
| --- | --- |
| `DATABASE_URL`, shared `DATA_DIR` | Postgres state and upload/package files accessible to both API and orchestrator. |
| `SHIZZLE_PIPELINE=cloud`, `SHIZZLE_EMBEDDED_ORCHESTRATOR=false` | Cloud processing with a separate orchestrator service. |
| `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID` | Submit/poll/cancel access to a configured lossless worker endpoint with available capacity. |
| AWS credentials, `AWS_REGION`, `S3_MEDIA_BUCKET` | Source/package access and browser-generation publication in private object storage. |
| `SHIZZLE_PASSCODE`, `TOKEN_SIGNING_SECRET` | Shared passcode gate and signed device tokens. |
| `CLOUDFRONT_DOMAIN`, `CLOUDFRONT_KEY_PAIR_ID`, `CLOUDFRONT_PRIVATE_KEY_PATH` | Expiring file-scoped media URLs; the signing key is mounted read-only. |

The API starts without running production migrations. The explicit deploy
transaction migrates Postgres before starting the release. The orchestrator
also never migrates production. `/api/health` reports database connectivity
and recent orchestrator heartbeat; it does not certify RunPod capacity,
delivery configuration, or successful end-to-end ingestion.

## Validation and retained testing

```powershell
uv run --directory library pytest -q -m "not postgres"
uv run --directory library pytest ../ops/tests/test_normalize_track_metadata.py -q
uv run --directory library ruff check .
uv run --directory library mypy src
```

These are the backend checks used by CI; mypy is currently informational
because existing strict typing errors remain. Tests use SQLite, S3 mocks,
fake provider responses, and generated media as appropriate. Availability of
FFmpeg/ffprobe determines whether media-specific checks can run.

The Postgres contract suite exercises real Alembic migrations, restart
recovery, expired leases, competing orchestrators, retry timing, duplicate
completion, publication refusal, and database constraints:

```powershell
docker compose -f deploy/vps/compose.yml --profile test -p shizzle-test up -d postgres
uv run --directory library pytest -q -m postgres
```

Use only a disposable test database. The fixture drops and recreates its
`public` schema before applying migrations. `SHIZZLE_TEST_DATABASE_URL`
overrides the default local test DSN on port 5433; check that it identifies
the isolated database. Ambient `POSTGRES_*` values affect both Compose and
the default test DSN. The suite skips when Postgres is unavailable.

Keep the browser scrubbing, continuous playback, stem-mix, natural-end/replay,
and bounded-fault procedures in
[`playback troubleshooting`](../docs/playback-troubleshooting.md). The
[`browser delivery contract`](../interfaces/shizzle-browser-v1/spec.md)
defines the encoding/alignment gates. Those browser procedures complement
backend tests; a unit-test pass does not prove live playback.
