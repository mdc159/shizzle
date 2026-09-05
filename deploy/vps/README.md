# Production VPS

Shizzle runs on a Hostinger KVM 4 VPS:

- Host: `72.60.173.171`
- Production directory: `/opt/shizzle/prod`
- Compose project: `shizzle`
- Public application: `https://shizzle.systems`
- Edge TLS and routing: Caddy

## Production services

- Caddy
- FastAPI
- Durable orchestrator
- Postgres

The VPS is the control plane. Normal browser media is delivered directly from
CloudFront rather than relayed through the VPS.

## Deployment and health

Production deploys run from the environment-gated
[deploy-vps workflow](../../.github/workflows/deploy-vps.yml), called after a
deployable master push passes CI. Documentation-only merges skip deployment.
It publishes a
digest-pinned API image, ships the player, updates `SHIZZLE_API_IMAGE` in the box's
`.env`, and checks the public application before succeeding. The host deploy
path transfers release assets with `scp` and extracts them with `tar`.

Use the production compose project name explicitly:

```text
cd /opt/shizzle/prod
docker compose -p shizzle -f compose.prod.yml ps
docker compose -p shizzle -f compose.prod.yml logs --tail 200 api orchestrator
```

For a manual local-source build, copy `compose.build.yml` from this directory
and place `library/` beside the compose files, then use the build overlay. The
API and orchestrator share the locally built image:

```text
SHIZZLE_API_IMAGE=shizzle-api:local-build docker compose -p shizzle -f compose.prod.yml -f compose.build.yml build api
SHIZZLE_API_IMAGE=shizzle-api:local-build docker compose -p shizzle -f compose.prod.yml -f compose.build.yml run --rm --no-deps api alembic upgrade head
SHIZZLE_API_IMAGE=shizzle-api:local-build docker compose -p shizzle -f compose.prod.yml -f compose.build.yml up -d
```

Rollback is transactional: the deploy records the prior files and Alembic
revision, and a failed startup or health check downgrades the database before
restoring the prior release. If downgrade fails, the prior files are restored
but application services remain stopped so an old image is not started against
an unknown schema state. If stopping the application fails, the prior files are
still restored but database downgrade and service restart are skipped for
manual recovery. For forward break-glass recovery, set
`SHIZZLE_API_IMAGE` in `/opt/shizzle/prod/.env` to a retained tag-plus-digest,
then migrate and recreate the services explicitly:

```text
docker compose -p shizzle -f compose.prod.yml run --rm --no-deps api alembic upgrade head
docker compose -p shizzle -f compose.prod.yml up -d --force-recreate --no-deps api orchestrator caddy
```

Before selecting an older image whose migration set predates the current
database revision, use the current image to downgrade explicitly to a revision
known by that older image.
Automated deployment fails closed while any `.rollback-*` or `.deploy-phase`
transaction artifact remains. After investigating an interrupted transaction,
restore it or explicitly remove all four artifacts before retrying; never let a
new deployment overwrite an unclosed rollback snapshot.
Automated deployment refuses an installation with no recorded prior API
identity ("Refusing automated first deployment"); bootstrap the first
production release explicitly by recording the identity of whatever image the
active stack is running:

```sh
api_image="$(docker inspect --format '{{.Config.Image}}' shizzle-api)" &&
orchestrator_image="$(docker inspect --format '{{.Config.Image}}' shizzle-orchestrator)" &&
test -n "$api_image" &&
test "$api_image" = "$orchestrator_image" &&
printf 'SHIZZLE_API_IMAGE=%s\n' "$api_image" >> /opt/shizzle/prod/.env
```

`docker inspect` deliberately avoids loading `compose.prod.yml`, which cannot
interpolate until this identity exists. The guarded command requires both
containers to report the same non-empty image reference, so the first automated
deployment can roll back to precisely the release that was active before it.
Done once per installation; every later deploy maintains the value itself.

## New-host setup

Use a Linux Docker host with Compose v2 supporting `env_file.required`, DNS
for both `shizzle.systems` and `www.shizzle.systems` pointing to the host, and
TCP ports 80/443 open. UDP 443 is optional for HTTP/3. The deployment workflow
currently connects as `root`; changing the SSH account requires matching
workflow and filesystem permissions.

Prepare `/opt/shizzle/prod` with `compose.prod.yml`, `Caddyfile` copied from
`Caddyfile.prod`, the built `player/dist` contents in `player/`, a private `.env`,
and `secrets/cloudfront_private_key.pem`. Use the repository `.env.example` as
a list to reconcile against current `Settings`; configure the passcode, token
signing secret, public origin, Postgres values, S3 bucket/credentials, and
CloudFront domain/key-pair identity. Both application containers require the
same `SHIZZLE_API_IMAGE`. Use a retained, published tag-plus-digest reference.
Production's Postgres and appdata are persistent named volumes.

The current Compose file requires the external volume
`shizzle-probe_caddy_data`. Preserve it on an existing host. On a genuinely new
host create that named volume before starting Caddy; its name is a compatibility
dependency and does not require running the old probe stack.

On the new Linux host, after configuration and image selection:

```sh
cd /opt/shizzle/prod
docker volume create shizzle-probe_caddy_data
docker compose -p shizzle -f compose.prod.yml config -q
docker compose -p shizzle -f compose.prod.yml pull
# --wait blocks until postgres is healthy; `run --no-deps` below skips depends_on.
docker compose -p shizzle -f compose.prod.yml up -d --wait postgres
docker compose -p shizzle -f compose.prod.yml run --rm --no-deps api alembic upgrade head
docker compose -p shizzle -f compose.prod.yml up -d
```

Set `SHIZZLE_PIPELINE=cloud`. Without RunPod credentials, existing media can
remain playable and the orchestrator heartbeat can be healthy, but new cloud
jobs cannot complete. Configure the [RunPod endpoint](../runpod/README.md)
for ingestion. `setup.sh` installs host packages and host security defaults;
it does not create this application release or configure S3/CloudFront.

## Health and media acceptance

The automated gate checks the public root, `/api/health` (`status=ok`,
`db=true`, `orchestratorAlive=true`), and unauthenticated `/cdn/tracks/x=403`.
It does not assert actual track playback or GPU readiness.

After bootstrap or a delivery change, authenticate through the browser, open
an existing track, verify a fresh manifest returns file-scoped media URLs,
and verify actual byte ranges return 206 with appropriate CORS/content types.
Play to natural end, scrub repeatedly in both directions, and inspect recovery
and telemetry using [the playback runbook](../../docs/playback-troubleshooting.md).
Avoid saving signed query credentials in test evidence.

## Bootstrap files

`setup.sh` is the host installer. `probe/` is a separate minimal service harness;
`compose.yml` is the development/reference stack, and `vm-test/` is the isolated
browser/relay test harness. Only `compose.prod.yml` is the production stack.

## Operational cautions

- Preserve the Caddy data volume across recreates.
- Keep Postgres private; only Caddy should publish public ports.
- Docker port publishing bypasses host firewall filtering unless explicitly
  constrained.
- Migrate routine deployment away from root access when a tested replacement
  path exists.
- Current status and next actions are in `../../docs/HANDOFF.md`.
