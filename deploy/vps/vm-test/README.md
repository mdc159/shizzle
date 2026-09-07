# SHIZZLE-TEST VM harness

Production-shaped stack for the isolated VMware VPS twin
(`D:\Virtual Machines\SHIZZLE-TEST`, host-only VMnet1, `192.168.176.52` —
read `D:\Virtual Machines\AGENTS.md` before touching the VM). Same topology
as production (postgres + api + orchestrator + caddy) minus Let's Encrypt and
CloudFront: pinned self-signed TLS on :443 so Secure-cookie auth behaves
exactly as on HTTPS production, and media served from the local appdata
volume.

The original local-media fixture is incompatible with the production library
filter (`local/` rows are hidden from `/api/library`); see the fixture
limitation below. Since 2026-09-07 the twin serves real cloud generations from
an in-VM MinIO instead, which makes it a working acceptance gate again — see
"S3 stand-in and cloud-track delivery".

The VM has no internet by design. Transfer the application image **and** the
Postgres/Caddy images referenced by `compose.vm.yml`, plus the built player,
over the host-only network. The following commands are for Bash on a Docker
host; substitute exact file paths when transferring from Windows:

```text
# on the host
docker build -f library/Dockerfile.api -t shizzle-api:vm-test library/
docker save shizzle-api:vm-test -o shizzle-api-vm-test.tar
docker pull postgres:16-alpine && docker pull caddy:2
docker save postgres:16-alpine -o postgres-16-alpine.tar
docker save caddy:2 -o caddy-2.tar
npm --prefix player run build && tar -czf player-dist.tgz -C player/dist .
scp shizzle-api-vm-test.tar postgres-16-alpine.tar caddy-2.tar player-dist.tgz deploy/vps/vm-test/compose.vm.yml deploy/vps/vm-test/Caddyfile .env mike@192.168.176.52:~/shizzle-test/

# in the VM (~/shizzle-test)
mkdir -p player
tar -xzf player-dist.tgz -C player
sudo docker load -i shizzle-api-vm-test.tar
sudo docker load -i postgres-16-alpine.tar
sudo docker load -i caddy-2.tar
mkdir -p certs && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout certs/key.pem -out certs/cert.pem -days 30 -nodes \
  -subj '/CN=192.168.176.52' -addext 'subjectAltName=IP:192.168.176.52'
sudo docker compose -p shizzle -f compose.vm.yml up -d --wait postgres
sudo docker compose -p shizzle -f compose.vm.yml run --rm --no-deps api alembic upgrade head
sudo docker compose -p shizzle -f compose.vm.yml up -d
```

`.env` (test-only values, never production secrets): `SHIZZLE_API_IMAGE`,
`SHIZZLE_PASSCODE`, `AUTH_VERSION`, `TOKEN_SIGNING_SECRET`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`, and `SHIZZLE_PIPELINE=cloud` (parked, with no
RunPod credentials). `compose.vm.yml` defaults to `test`, which now fails closed
unless `SHIZZLE_ALLOW_TEST_PIPELINE=1` is explicitly set. The test stub produces
no playable media; only use that opt-in for intentional orchestrator drills.

## S3 stand-in and cloud-track delivery (added 2026-09-07)

The VM carries a MinIO container (`minio`, bucket `karaoke-pimpshizzle`) left
over from the 2026-08-12 lossless-intake proving run. Since 2026-09-07 it is the
stack's S3 and the twin can serve real cloud generations, which is what the
production library filter (C7) requires:

- `minio` is attached to the compose network (`docker network connect
  shizzle_default minio`) so `api`/`orchestrator` resolve `minio`.
- `~/shizzle-test/.env` sets `AWS_ENDPOINT_URL=http://minio:9000`,
  `AWS_REGION`, `S3_MEDIA_BUCKET=karaoke-pimpshizzle` and the MinIO root
  credentials (test-only values; never production keys).
- `Caddyfile` maps the same-origin media path to the bucket in place of
  CloudFront:

  ```text
  handle_path /cdn/* {
      rewrite * /karaoke-pimpshizzle{uri}
      reverse_proxy minio:9000
  }
  ```

  `handle_path` is required: Caddy runs `rewrite` before `uri`, so
  `handle /cdn/*` + `uri strip_prefix` forwards the wrong key.
- Published generations are anonymously readable
  (`mc anonymous set download local/karaoke-pimpshizzle/tracks`); everything
  else in the bucket, including `imports/`, stays private.
- A cloud track is registered with `TrackRepository.upsert_imported` (or by the
  drop-box ingest, see
  [contributing completed media](../../../docs/contributing-completed-media.md));
  `/api/tracks/{id}/manifest` then rewrites media to `/cdn/...` exactly as
  production does without CloudFront cookies.

Rebuilding the api image offline: the image installs the project editable from
`/app/src`, so a derived image that only overlays source (`FROM
shizzle-api:vm-test` + `COPY src /app/src`) is enough while the branch adds no
dependencies; `~/build-overlay/Dockerfile` on the VM does exactly that.

Run the production playback harness against the twin with
`SHIZZLE_E2E_IGNORE_HTTPS_ERRORS=1` (self-signed certificate) and
`SHIZZLE_E2E_BASE_URL=https://192.168.176.52`.

Snapshots: `shizzle-stack-v1` (2026-08-17, local-profile fixture only),
`shizzle-stack-v2-minio-cdn` (2026-09-07, this configuration, taken live) and
`shizzle-stack-v3-imperial-march` (2026-09-07, plus the drop-box-ingested
Imperial March and the current player bundle; stress/natural/faults passed).
The VM folder's `SHIZZLE-TEST-HANDOFF.md` is the authoritative record of the
machine itself; keep both in step.

## Media fixture limitation

The prior fixture copied a generation under `/app/data/<dir>`, renamed its
manifest to `stems.json`, and inserted a track with `s3_prefix=local/<dir>`.
The current `TrackRepository.list_tracks` admits only `tracks/` prefixes
(invariant C7), so that row cannot be selected by the library drawer. Simply
renaming its prefix to `tracks/` does not fix it: manifest loading then uses S3
instead of the local-media route. A supported isolated fixture/storage adapter
is needed before this offline setup is reproducible. Do not relax the production
publication guard or copy production credentials into the VM to bypass it.

## Retained browser acceptance procedure

Against a deployment with a selectable, playable **Black Hole Sun** fixture,
run the full test from the host. The spec currently hardcodes that title. It
uses two browser contexts and the real relay, and verifies mute in measured
post-gain PCM while the remaining mix keeps playing:

```text
cd player && SHIZZLE_E2E_BASE_URL=https://192.168.176.52 \
  SHIZZLE_E2E_PASSCODE=<passcode> SHIZZLE_E2E_HEADLESS=1 \
  npx playwright test e2e/remote-mixer-vm.spec.ts
```

Keep the browser video staging request's same-origin credentials behavior:
local-profile media requires the device-token cookie, and `/cdn` fallback
requires the CloudFront cookies. The separate
[playback runbook](../../../docs/playback-troubleshooting.md) retains natural-end,
repeated scrubbing, continuity, and audio-quality checks.
