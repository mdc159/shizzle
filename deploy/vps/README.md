# Production VPS

Shizzle runs on a Hostinger KVM 4 VPS:

- Host: `72.60.173.171`
- Production directory: `/opt/shizzle/prod`
- Compose project: `shizzle`
- Public application: `https://shizzle.systems`
- Edge TLS and routing: Caddy
- Staging directory: `/opt/shizzle/staging` (Compose project
  `shizzle-staging`) — see [Staging stack](#staging-stack) below

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

## Admin listener

`/api/admin/*` and the `/admin` page are not public: `Caddyfile.prod` answers
404 for them on `shizzle.systems` and strips any incoming `X-Shizzle-Admin`
request header. The admin surface is served by a second plain-HTTP Caddy
listener published only on `127.0.0.1:18081`, which injects
`X-Shizzle-Admin: $SHIZZLE_ADMIN_PROXY_SECRET` upstream; the value lives in
`/opt/shizzle/prod/.env` (fail-closed: with it unset every admin route is a
404 everywhere). The host fronts it with
`tailscale serve --bg --https=8444 http://127.0.0.1:18081`, so the admin URL
is `https://shizzle.tailfedd3b.ts.net:8444` — tailnet only, real TLS.
Undo with `tailscale serve --https=8444 off`.

## Staging stack

A full copy of the site runs beside production in `/opt/shizzle/staging`
(Compose project `shizzle-staging`): same four services, own `.env` (template
`deploy/vps/.env.staging.example`), own Postgres/job-data volumes, own player
directory, digest-pinned api image, CPU+memory limits on every container,
and a single published port `127.0.0.1:18080:80` (plain-HTTP Caddy). The host
fronts it with `tailscale serve --bg --https=8443 http://127.0.0.1:18080`,
so staging is `https://shizzle.tailfedd3b.ts.net:8443` — tailnet only, real
TLS (auth cookies are Secure). Undo with `tailscale serve --https=8443 off`.
Do not disturb the pre-existing `tailscale serve` on 443 (proxy to
`127.0.0.1:3773`); the 8443/8444 ports were chosen to leave it alone.

Staging deploys run from the manual
[deploy-staging workflow](../../.github/workflows/deploy-staging.yml): pick a
branch or commit, it reuses `api-image.yml` for the digest-pinned image,
ships the player/compose/Caddyfile plus `deploy-staging.sh`, and runs the
transaction on the box (pull, validate, migrate, start, health check on
`127.0.0.1:18080`). Rollback is simple: on failure the script restores the
prior files and restarts the previous image or stops the app services; the
database is never downgraded, so an old image may meet a newer schema on
staging — acceptable there, repaired by redeploying the newer ref.

Data isolation, in one paragraph each:

- **Media.** Staging uses its own S3 bucket (`S3_MEDIA_BUCKET`) behind its own
  CloudFront distribution (OAC + key group; the private key sits at
  `/opt/shizzle/staging/secrets/cloudfront_private_key.pem`). Manifests then
  carry file-scoped signed CloudFront URLs (E5) fetched directly by the
  browser, so staging's Caddyfile has no `/cdn` proxy; without the CloudFront
  variables the site runs but media will not play. The bucket's CORS
  configuration must allow the tailnet origins
  `https://shizzle.tailfedd3b.ts.net:8443` and `:8444` (media elements use
  `crossOrigin="anonymous"`).
- **Separation.** RunPod separation is real: the dispatch payload names the
  staging bucket (`job_input.bucket`), so the endpoint worker's IAM policy
  must allow read/write on it, or staging needs its own endpoint.
- **Nodes.** `SHIZZLE_NODE_TOKENS` stays empty on staging, so nodes cannot
  authenticate against it (E6) and production keeps the Mac mini and Dell.
  URL jobs therefore wait `NODE_DOWNLOAD_WAIT_SECONDS` and fail
  `NO_NODE_AVAILABLE`; fixing that takes a node agent repointed at the
  staging origin with staging-only credentials
  ([docs/WORKER-NODES.md](../../docs/WORKER-NODES.md)).
- **Admin.** Staging's listener injects the same `X-Shizzle-Admin` header, so
  `/admin` works on staging with the staging `.env`'s secret value.

### Bringing staging up (ordered, one-time)

On AWS:

1. Create the staging bucket (e.g. `shizzle-staging-media`, private,
   block-public-access on) in `us-east-1`; add bucket CORS allowing GET/HEAD
   from `https://shizzle.tailfedd3b.ts.net:8443` and `:8444`.
2. Create an RSA CloudFront signing key pair (or reuse the production key
   material as a *new* public-key resource with its own key-pair id); create
   a key group containing it.
3. Create the staging CloudFront distribution: origin = the staging bucket
   via OAC, default behavior `tracks/*` (or default `*`) with Origin Request
   and Cache policies like production, Restrict Viewer Access = trusted key
   group from step 2, Range/GET/HEAD only. Note its `dXXXX.cloudfront.net`
   domain and key-pair id.
4. Ensure the RunPod endpoint worker's IAM policy covers the staging bucket
   (read source, write attempts) if staging will separate uploads.
5. Give the staging `.env` an IAM key (new staging user, or widen the existing
   one) allowed on the staging bucket only.

On the VPS (as root):

```sh
mkdir -p /opt/shizzle/staging/secrets
cd /opt/shizzle/staging
# from a checkout of this repo at the deployed ref:
cp deploy/vps/.env.staging.example .env        # then edit secrets/values below
chmod 600 .env
# set: POSTGRES_PASSWORD, SHIZZLE_PASSCODE, TOKEN_SIGNING_SECRET,
#      SHIZZLE_ADMIN_PROXY_SECRET, AWS keys, S3_MEDIA_BUCKET,
#      CLOUDFRONT_DOMAIN + CLOUDFRONT_KEY_PAIR_ID, RUNPOD_*
#      (SHIZZLE_NODE_TOKENS stays empty)
openssl genrsa -out secrets/cloudfront_private_key.pem 2048   # if a new key pair
chmod 600 secrets/cloudfront_private_key.pem
# (its public key is what you registered in the key group in AWS)
tailscale serve --bg --https=8443 http://127.0.0.1:18080
tailscale serve --bg --https=8444 http://127.0.0.1:18081   # admin; also add
#   SHIZZLE_ADMIN_PROXY_SECRET to /opt/shizzle/prod/.env and recreate caddy:
#   cd /opt/shizzle/prod && docker compose -p shizzle -f compose.prod.yml up -d --force-recreate --no-deps caddy api
```

In GitHub (repository settings, not the production environment):

6. Add repository secrets `VPS_STAGING_HOST` (`72.60.173.171`),
   `VPS_STAGING_SSH_KEY` (same key material as production unless a dedicated
   account exists), `VPS_STAGING_SSH_HOST_KEY` (same pinned host key:
   `ssh-keyscan -t ed25519 72.60.173.171`).

Then dispatch the `deploy-staging` workflow with `master` (or any branch);
it bootstraps postgres, migrates, starts, health-checks, and staging is
live at `https://shizzle.tailfedd3b.ts.net:8443`. The player's API base is
relative, so no application setting names the tailnet origin.

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
browser/relay test harness. Only `compose.prod.yml` is the production stack;
`compose.staging.yml` (with `Caddyfile.staging` and `.env.staging.example`)
forms the staging stack described above.

## Operational cautions

- Preserve the Caddy data volume across recreates.
- Keep Postgres private; only Caddy should publish public ports.
- Docker port publishing bypasses host firewall filtering unless explicitly
  constrained. The staging (127.0.0.1:18080) and admin (127.0.0.1:18081)
  listeners rely on the explicit loopback host IP in `ports:` — never widen
  those entries, and re-run `deploy/vps/tests/test_staging_stack.sh` if you
  touch them.
- Staging containers are resource-limited; keep limits on any new staging
  service so staging cannot starve production.
- Migrate routine deployment away from root access when a tested replacement
  path exists.
- Current status and next actions are in `../../docs/HANDOFF.md`.
