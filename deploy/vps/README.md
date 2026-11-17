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
`/opt/shizzle/prod/.env`. The API-side check of that header is a separate
application change and is **not present in this revision**; do not assume the
proxy secret alone enforces application authorization. When that gate lands,
an empty secret must keep every admin route fail-closed. The host fronts it with
`tailscale serve --bg --https=8444 http://127.0.0.1:18081`, so the admin URL
is `https://shizzle.tailfedd3b.ts.net:8444` — tailnet only, real TLS.
Undo with `tailscale serve --https=8444 off`.

One-time cleanup after the release that carries `persist_config off`: the
production `caddy_config` volume may still hold `/config/caddy/autosave.json`
written by an older Caddyfile — it can contain the substituted admin secret
in plain text (E3). Check and remove it (safe: both stacks always start Caddy
with an explicit `--config`):

```sh
docker exec shizzle-caddy sh -c 'ls -l /config/caddy/autosave.json 2>/dev/null || echo none'
docker exec shizzle-caddy rm -f /config/caddy/autosave.json
# staging's volume is new, but the same check applies if it predates the setting:
docker exec shizzle-staging-caddy sh -c 'ls -l /config/caddy/autosave.json 2>/dev/null || echo none'
```

Note for integration review: cookies are host-scoped, not port-scoped, so a
`Secure` cookie set on 8443 is also sent to 8444 on the same tailnet host.
Both listeners are tailnet-only and the player authenticates primarily with
an origin-scoped bearer token, but keep this in mind when adding new
cookie-bearing routes on either port.

The listener appears only after a production release carrying the new
`compose.prod.yml` and `Caddyfile.prod` has deployed through the approved
production workflow (see the staging bring-up checklist for the one-time
`.env` + recreate step that follows it).

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
branch or commit; the player is built from that ref in a job with no VPS
credentials, the digest-pinned image comes from `api-image.yml` pinned to
reviewed default-branch commit `b6cf7129606bb0b903daa709ddf7a7dd3b409d46`
(not from the dispatched or selected feature ref), and the deploy job ships the player artifact plus `deploy-staging.sh`,
`compose.staging.yml` and `Caddyfile.staging` taken from the **default
branch** (a change to those deploy assets reaches staging only after it is
merged). The deploy job runs under the `staging` GitHub environment, which
holds the SSH secrets and whose **deployment-branch rule must allow the
default branch only** (required setup — see the checklist below; a required
reviewer is the optional stronger setting). The transaction on
the box is pull, validate, migrate, start, then require both a healthy
`127.0.0.1:18080/api/health` response and a service heartbeat attributable
to the replacement orchestrator container; the first run bootstraps a fresh
directory. Failures before any active-file mutation leave the current files
and running bind mounts untouched. Once mutation begins, rollback restores
the prior files and recreates the previous image so Caddy rebinds the restored
player; fully activated failures are stopped first, and a failed first deploy
returns the directory to its fresh state. A failed restore or restart is nonzero and retains
`.staging-rollback.tar.gz`; any stale rollback archive or temporary archive
blocks the next deploy until an operator recovers or explicitly removes it.
The database is never downgraded, so an old image may meet a newer schema on
staging — acceptable there, repaired by redeploying the newer ref. Honest boundary: the chosen ref's image still runs on this
production host, and the staging SSH identity is root here — see
[AUTOMATION §3b](../../docs/AUTOMATION.md) before assuming isolation.

Data isolation, in one paragraph each:

- **Media.** Staging uses its own S3 bucket (`S3_MEDIA_BUCKET`) behind its own
  CloudFront distribution (OAC + key group; the private key sits at
  `/opt/shizzle/staging/secrets/cloudfront_private_key.pem`). Manifests then
  carry file-scoped signed CloudFront URLs (E5) fetched directly by the
  browser, so staging's Caddyfile has no `/cdn` proxy; without the CloudFront
  variables the site runs but media will not play. The staging bucket's CORS
  configuration must allow `https://shizzle.tailfedd3b.ts.net:8443` because
  media elements use `crossOrigin="anonymous"`. Production's separate bucket
  needs its own `:8444` admin origin; see step 7 below.
- **Separation.** RunPod separation is real: the dispatch payload names the
  staging bucket (`job_input.bucket`), so the endpoint worker's IAM policy
  must allow read/write on it, or staging needs its own endpoint.
- **Nodes.** `SHIZZLE_NODE_TOKENS` stays empty on staging, so nodes cannot
  authenticate against it (E6) and production keeps the Mac mini and Dell.
  URL jobs therefore wait `NODE_DOWNLOAD_WAIT_SECONDS` and fail
  `NO_NODE_AVAILABLE`; fixing that takes a node agent repointed at the
  staging origin with staging-only credentials
  ([docs/WORKER-NODES.md](../../docs/WORKER-NODES.md)).
- **Admin.** Staging's listener injects the same `X-Shizzle-Admin` header
  with the staging `.env`'s secret value. The API-side enforcement is pending
  the separate application change noted above.

### Bringing staging up (ordered, one-time)

On AWS (order matters — the signing key exists before its public key is
registered, and the bucket policy is applied after the distribution exists
because it is scoped to the distribution ARN):

1. Create the staging bucket (e.g. `shizzle-staging-media`) in `us-east-1`,
   private, with all four block-public-access flags on.
2. Generate the signing key pair NOW (before step 3 registers its public
   key), and keep the private key for the VPS step below:
   ```sh
   openssl genrsa -out cf-staging-private-key.pem 2048
   openssl rsa -in cf-staging-private-key.pem -pubout -out cf-staging-public-key.pem
   ```
3. CloudFront → Public keys: upload `cf-staging-public-key.pem`; note the
   **key-pair id** (`K…`). Create a key group (e.g. `staging-media-signing`)
   containing that public key.
4. Create the staging CloudFront distribution: origin = the staging bucket
   **via Origin Access Control** (create the OAC setting, origin type S3,
   sigv4); default behavior allowed methods GET/HEAD (Range included
   automatically), Restrict Viewer Access = Yes with the trusted key group
   from step 3. Cache configuration: create (or choose) a cache policy that
   includes the headers `Origin`, `Access-Control-Request-Method` and
   `Access-Control-Request-Headers` in the cache key (CORS-safe); do NOT
   attach a response-headers policy that overrides CORS — the bucket's own
   CORS answers. Note the distribution **domain** (`dXXXX.cloudfront.net`)
   and its **ARN** (`arn:aws:cloudfront::<account-id>:distribution/<id>`).
5. Apply the OAC bucket policy on the staging bucket (S3 still denies
   CloudFront without it — signed viewer requests would get origin 403s).
   Substitute your bucket name and distribution ARN:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [{
       "Sid": "AllowCloudFrontStagingRead",
       "Effect": "Allow",
       "Principal": { "Service": "cloudfront.amazonaws.com" },
       "Action": "s3:GetObject",
       "Resource": "arn:aws:s3:::shizzle-staging-media/tracks/*",
       "Condition": { "StringEquals": { "AWS:SourceArn": "arn:aws:cloudfront::YOUR_ACCOUNT:distribution/YOUR_DISTRIBUTION_ID" } }
     }]
   }
   ```
6. Staging bucket CORS (exact configuration — the staging tailnet origin,
   because media elements use `crossOrigin="anonymous"`):
   ```json
   [
     {
       "AllowedHeaders": ["*"],
       "AllowedMethods": ["GET", "HEAD"],
       "AllowedOrigins": [
         "https://shizzle.tailfedd3b.ts.net:8443"
       ],
       "ExposeHeaders": ["ETag", "Content-Range", "Content-Length"],
       "MaxAgeSeconds": 3000
     }
   ]
   ```
7. Production admin playback uses production manifests and direct production
   CloudFront URLs, not the staging bucket. Add
   `https://shizzle.tailfedd3b.ts.net:8444` to the **production bucket's**
   existing CORS `AllowedOrigins`, preserving its current origins, methods,
   headers and exposed headers. If the production distribution has a
   response-headers policy that overrides origin CORS, allow that origin
   there too. Verify from the admin browser, or with a fresh production
   signed media URL:
   ```sh
   curl -sS -D - -o /dev/null \
     -H 'Origin: https://shizzle.tailfedd3b.ts.net:8444' \
     -H 'Range: bytes=0-1023' '<production-signed-url>'
   ```
   Require `206` and `Access-Control-Allow-Origin:
   https://shizzle.tailfedd3b.ts.net:8444`; a `206` without that header still
   fails browser media playback. Do not record the signed query credentials.
8. If staging will separate uploads, give the RunPod endpoint worker's IAM
   principal exactly the S3 access the worker code performs on the staging
   bucket (derived from `stemsplit/s3_ops.py` and
   `stemsplit/lossless_handler.py`): it reads the job source (boto3
   `head_object` for the size probe, then `download_file`); writes the
   attempt package under
   `tracks/<track>/1/separation/attempts/<sha256>/` — conditional receipts
   and handoff (`put_object` with `IfNoneMatch`/`IfMatch`), stems via
   multipart `upload_file` (Create/UploadPart/Complete are all authorized by
   `s3:PutObject`, abort needs its own action), and it re-reads its own
   receipt/handoff and HEADs uploaded stems to verify ETags. There is no
   `s3:HeadObject` IAM action: every HEAD above is authorized by
   `s3:GetObject`. `s3:ListBucket` is required on the bucket itself and must
   be UNCONDITIONED: on a first attempt the handler GETs the absent
   `handoff.json` to detect completion (lossless_handler.py `_is_not_found`
   accepts only 404/NoSuchKey); without an effective ListBucket grant S3
   answers that probe with 403, `_is_not_found` is false, and the handler
   re-raises — the first attempt can never start. S3 decides 404-vs-403 for
   that GetObject by evaluating s3:ListBucket in the GetObject request
   context, which carries no `s3:prefix` (that condition key exists only in
   ListObjects requests), so a prefix-conditioned ListBucket grant never
   matches the probe and changes nothing. The grant therefore covers the
   whole bucket — it lets the worker list keys anywhere in this dedicated
   staging bucket, which is the price of correct 404s:
   ```json
   {
     "Version": "2012-10-17",
     "Statement": [
       {
         "Sid": "ListForAbsentHandoffAndSourceChecks",
         "Effect": "Allow",
         "Action": ["s3:ListBucket"],
         "Resource": "arn:aws:s3:::shizzle-staging-media"
       },
       {
         "Sid": "ReadJobSources",
         "Effect": "Allow",
         "Action": ["s3:GetObject"],
         "Resource": "arn:aws:s3:::shizzle-staging-media/sources/*"
       },
       {
         "Sid": "WriteSeparationAttempts",
         "Effect": "Allow",
         "Action": ["s3:GetObject", "s3:PutObject", "s3:AbortMultipartUpload"],
         "Resource": "arn:aws:s3:::shizzle-staging-media/tracks/*/1/separation/attempts/*"
       }
     ]
   }
   ```
   (Object permissions stay prefix-scoped; the worker never deletes; handoff
   .json lands last under `IfNoneMatch`, invariant A1.)
9. Give the staging `.env` an IAM key (a new staging-only user is cleanest)
   allowed on the staging bucket only (`s3:*` on
   `arn:aws:s3:::shizzle-staging-media/*` plus `s3:ListBucket`).

On the VPS (as root; `CHECKOUT` is your absolute path to a checkout of this
repository — use an absolute path, the commands `cd` first):

```sh
CHECKOUT=/root/shizzle   # absolute path to a checkout of this repo
mkdir -p /opt/shizzle/staging/secrets
cd /opt/shizzle/staging
cp "$CHECKOUT/deploy/vps/.env.staging.example" .env   # then edit as below
chmod 600 .env
# set in .env: POSTGRES_PASSWORD, SHIZZLE_PASSCODE, TOKEN_SIGNING_SECRET,
#      DATABASE_URL (same credentials, URL-encoded userinfo), AWS keys,
#      SHIZZLE_ADMIN_PROXY_SECRET, S3_MEDIA_BUCKET=shizzle-staging-media,
#      CLOUDFRONT_DOMAIN=<dXXX from step 4>, CLOUDFRONT_KEY_PAIR_ID=<K… from step 3>,
#      RUNPOD_* if staging separates (SHIZZLE_NODE_TOKENS stays empty)
# URL-encode a raw password without putting it in shell history; paste the
# result into the password component of DATABASE_URL in .env.
python3 - <<'PY'
from getpass import getpass
from urllib.parse import quote
print(quote(getpass('Raw PostgreSQL password: '), safe=''))
PY
# DATABASE_URL shape (use the actual encoded values, not this placeholder):
# postgresql+asyncpg://shizzle:URL_ENCODED_PASSWORD@postgres:5432/shizzle
install -m 600 /path/to/cf-staging-private-key.pem secrets/cloudfront_private_key.pem
tailscale serve --bg --https=8443 http://127.0.0.1:18080
tailscale serve --bg --https=8444 http://127.0.0.1:18081
```

Production's admin listener (`:8444`) exists only after a production release
carrying the new `compose.prod.yml` and `Caddyfile.prod` has been deployed
through the normal approved workflow — until then `tailscale serve …8444`
fronts nothing on 18081. After such a release, also add
`SHIZZLE_ADMIN_PROXY_SECRET` to `/opt/shizzle/prod/.env` and recreate the
services:

```sh
cd /opt/shizzle/prod && docker compose -p shizzle -f compose.prod.yml \
  up -d --force-recreate --no-deps caddy api
```

In GitHub (repository settings → Environments — NOT the production
environment):

10. Create an environment named `staging` and add environment secrets
   `VPS_STAGING_HOST` (`72.60.173.171`), `VPS_STAGING_SSH_KEY` (root key
   material — the same trust level as the production deploy key, not a lesser
   one), `VPS_STAGING_SSH_HOST_KEY` (same pinned host key:
   `ssh-keyscan -t ed25519 72.60.173.171`).
11. REQUIRED (the workflow cannot substitute for this): set the `staging`
   environment's deployment-branch rule to allow the **default branch only**.
   A manual dispatch runs the *dispatched ref's copy* of the workflow file,
   so a feature branch could otherwise rewrite the deploy job and still read
   the environment's secrets; the branch rule is the one control such a run
   cannot edit. The workflow's dispatch-ref guard (it fails unless dispatched
   from the default branch) is a convenience that points at correct usage —
   the rule is the control. The policy itself is configured on the
   environment (PUT /repos/{owner}/{repo}/environments/staging with nested
   JSON); individual branch rules are managed through
   /deployment-branch-policies (plural), and the settings are read back
   through the environment GET:
   ```sh
   # Configure the policy. This is a create-or-update of the environment:
   # if it already has reviewers or a wait timer you want to keep, read it
   # first (the GET below) and include them in this same JSON.
   gh api -X PUT repos/mdc159/shizzle/environments/staging --input - <<'EOF'
   {"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}
   EOF
   # Allow exactly the default branch:
   gh api -X POST repos/mdc159/shizzle/environments/staging/deployment-branch-policies \
     -f name=master
   # Verify the settings:
   gh api repos/mdc159/shizzle/environments/staging --jq .deployment_branch_policy
   # expect: {"custom_branch_policies":true,"protected_branches":false}
   # Verify the actual default branch is the sole allowed rule (branch rules
   # match branch/tag names by pattern, so any extra entry widens access):
   gh api repos/mdc159/shizzle/environments/staging/deployment-branch-policies \
     --jq '.branch_policies[] | "\(.id) \(.name)"'
   # expect exactly one line naming master; remove any other rule by id:
   gh api -X DELETE repos/mdc159/shizzle/environments/staging/deployment-branch-policies/<ID>
   ```
   A required reviewer on the environment is the optional stronger setting —
   every staging deploy then pauses for a human.

Then dispatch the `deploy-staging` workflow **from `master` (the default
branch)** and pass the branch or commit to deploy in its **`ref` input**;
it bootstraps postgres, migrates, starts, health-checks, and staging is
live at `https://shizzle.tailfedd3b.ts.net:8443`. The player's API base is
relative, so no application setting names the tailnet origin.

Confirm media end to end once a track exists: from an authenticated browser
tab on staging, copy a file URL out of
`https://shizzle.tailfedd3b.ts.net:8443/api/tracks/<id>/manifest`, then

```sh
curl -so /dev/null -w '%{http_code}\n' -H 'Range: bytes=0-1023' '<signed-url>'
```

must print `206` (signed Range through the staging distribution; `403` means
the bucket policy from step 5 or the key group from step 3 is wrong).

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
- Caddy publishes 80 and 443 on the public address only
  (`SHIZZLE_PUBLIC_IP`, default `72.60.173.171`), not on every address.
  Tailscale Serve on the same host uses port 443 of the tailnet address and
  takes it first after a reboot; a `0.0.0.0:443` publish then fails with
  `address already in use` and the site stays down until Caddy is recreated
  by hand. After changing either side, reboot the host and confirm the site
  returns unaided.
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
