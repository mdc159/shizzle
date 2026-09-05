# Setup

## What is required

| Use | Requirements |
|---|---|
| Work on API/UI | Git, uv, Python 3.11 or newer, Node.js 22/npm, FFmpeg and ffprobe on PATH |
| Run unit and mocked UI tests | The above; Playwright Chromium; see [Testing](TESTING.md) |
| Test database coordination | Isolated Postgres 16, normally Docker Compose; never use the production database |
| Run cloud ingestion | VPS API/orchestrator, shared job storage, Postgres, private S3 credentials, configured RunPod lossless endpoint with available workers |
| Deliver browser media | CloudFront with private S3 origin access, trusted signing key, Range/CORS support; HTTPS app origin |
| Build/operate production | Linux Docker/Compose host, DNS/TLS, GHCR image access, GitHub Actions/environment configuration and SSH trust |

Production uses `stemsplit/Dockerfile.lossless` for separation and
`library/Dockerfile.api` for the API/orchestrator. The slim API image includes
FFmpeg, not a GPU separator. The older GPU/local/reference images are retained
for tests and comparison and are not the production cloud worker.

## Local API and player

Run from the repository root in PowerShell. These environment values select a
local SQLite database under `data/dev-review` and disable automatic processing; the API can be
used for UI/API work without dispatching GPU jobs.

```powershell
uv sync --directory library --frozen
$env:DATA_DIR = Join-Path (Get-Location) 'data\dev-review'
New-Item -ItemType Directory -Force $env:DATA_DIR | Out-Null
$env:DATABASE_URL = 'sqlite+aiosqlite:///' + ((Join-Path $env:DATA_DIR 'shizzle.db') -replace '\\', '/')
$env:SHIZZLE_EMBEDDED_ORCHESTRATOR = 'false'
$env:SHIZZLE_PIPELINE = 'cloud'
$env:SHIZZLE_PASSCODE = ''
uv run --directory library uvicorn shizzle_server.main:app --host 127.0.0.1 --port 8001
```

In another terminal:

```powershell
npm --prefix player ci
npm --prefix player run dev
```

Open `http://localhost:5173`. If prompted, enter any nonempty value, such as
`dev`: the UI still requires an entry, while this local API has passcode checking
disabled. Vite proxies `/api` (including WebSocket upgrades) to
`localhost:8001`. A newly created database has an empty library; uploads stay pending with the
orchestrator disabled. `/api/health` will report the missing orchestrator, which
is expected for this API-only mode. SQLite initializes its own tables.

These values apply to the current terminal. Runtime `Settings` reads process
environment; a bare `uv run` does not automatically load the repository `.env`.
The server is intentionally unauthenticated only on loopback for this recipe.
Mocked UI tests supply their own API responses and do not require importing a
production library.

## Configuration

For Compose, copy `.env.example` to a new gitignored `.env` and fill the values
for the target environment. Keep an existing `.env` intact. Shell environment
variables take precedence over Compose interpolation, even with `--env-file`.
The template is not a live deployment snapshot and contains no usable secrets.

| Variables | Used for |
|---|---|
| `SHIZZLE_PASSCODE`, `TOKEN_SIGNING_SECRET`, `AUTH_VERSION` | Device tokens; empty passcode disables auth. Changing the passcode revokes existing tokens. |
| `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Compose Postgres and constructed service DSNs |
| `DATABASE_URL`, `DATA_DIR` | Native API/orchestrator database and shared job working directory |
| `SHIZZLE_PIPELINE=cloud`, `SHIZZLE_EMBEDDED_ORCHESTRATOR=false` | Production cloud stages and separate orchestrator process |
| `RUNPOD_API_KEY`, `RUNPOD_ENDPOINT_ID` | RunPod dispatch/poll/cancel; both required for new cloud work |
| `AWS_REGION`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `S3_MEDIA_BUCKET` | Private source/package/delivery object storage |
| `AWS_ENDPOINT_URL` | Explicit S3-compatible override; leave empty for AWS and check ambient overrides |
| `CLOUDFRONT_DOMAIN`, `CLOUDFRONT_KEY_PAIR_ID`, `CLOUDFRONT_PRIVATE_KEY_PATH` | File-scoped signed media URLs and fallback cookies |
| `MEDIA_TTL_SECONDS`, `MEDIA_COOKIE_PATH` | Media expiry (86400 seconds default) and `/cdn` fallback path |
| `SHIZZLE_API_IMAGE` | Digest-pinned API image required by production Compose |
| `MAX_UPLOAD_BYTES`, `MAX_DURATION_SECONDS` | Upload copy and successful-duration-probe limits |

Use the bucket's actual AWS region. This repository defaults to `us-east-1`;
that is independent of a developer's general AWS-region preference. CloudFront
key files are mounted read-only into the API; never put private keys into Git.
Worker settings and its image identity are documented in
[RunPod setup](../deploy/runpod/README.md).

## Production commissioning

1. Prepare private S3 source/package/delivery access and CloudFront origin
   access, signing keys, CORS and Range behavior. Review
   [AWS setup](../deploy/aws/README.md); there is no complete automated
   provisioner for the current topology in this repository.
2. Prepare the Linux VPS, persistent volumes, DNS/TLS, secret file and release
   directory using [VPS setup](../deploy/vps/README.md). The checked-in production
   Compose expects an existing external Caddy certificate volume.
3. Configure GitHub credentials, environment approval, SSH host trust and registry
   access following [Automation](AUTOMATION.md). Build the UI and digest-pinned
   API image through the supported release workflow. Apply schema migration
   `0005_job_artist` through the explicit deploy transaction.
4. Publish the lossless GPU image using `worker-image.yml`, configure/repoint the
   RunPod template, and verify endpoint configuration. Repointing intentionally
   parks the pool; enabling workers is a separate operational action.
5. Set both RunPod credentials on the orchestrator, select the cloud pipeline,
   and verify API, database and orchestrator health. Healthy control-plane status
   alone does not prove that a GPU worker can run or media can play.
6. As an intentional acceptance run, upload `fixtures/golden-30s.mp4`. Require
   the job to become `ready`, verify the selected lossless attempt and published
   generation, and play it through the actual browser. This step creates a job,
   storage objects and potentially billed GPU work.
7. Run the relevant [browser continuity and listening checks](TESTING.md), and
   record the deployed revision, image and sanitized evidence.

Use `docker compose -p shizzle -f compose.prod.yml` from the production release
directory for inspection. Do not substitute `up` for the release transaction:
it does not take a rollback snapshot or migrate the database. API and orchestrator
must use the same image and shared data volume.

## Supported boundaries

URL acquisition remains unavailable. `local` mode requires FFmpeg/Demucs and
retains reference processing; `test` additionally requires
`SHIZZLE_ALLOW_TEST_PIPELINE=1` and produces no media. Neither provides accepted
cloud publication under C7. The [VM harness](../deploy/vps/vm-test/README.md)
also has compatibility limitations recorded in the review.

An installed dependency, passing unit test, healthy endpoint or old successful
run does not establish current end-to-end operation. Use the
[review issue list](REVIEW.md) when planning fixes and acceptance runs.
