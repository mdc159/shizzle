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
[deploy-vps workflow](../../.github/workflows/deploy-vps.yml). It publishes a
digest-pinned API image, ships the player, updates `SHIZZLE_API_IMAGE` in the box's
`.env`, and checks the public application before succeeding. The host deploy
path transfers release assets with `scp` and extracts them with `tar`.

Use the production compose project name explicitly:

```text
cd /opt/shizzle/prod
docker compose -p shizzle ps
docker compose -p shizzle logs --tail 200 api orchestrator
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
an unknown schema state. For forward break-glass recovery, set
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
identity; bootstrap the first production release explicitly.

## Current work

Playback delivery is complete. The active infrastructure work is connecting the
orchestrator to dependable cloud source acquisition and cloud GPU separation.

## Bootstrap files

`setup.sh` and `probe/` record the original host bootstrap and minimal service
probe. They are retained for recovery and troubleshooting; they are not the
current application stack.

## Operational cautions

- Preserve the Caddy data volume across recreates.
- Keep Postgres private; only Caddy should publish public ports.
- Docker port publishing bypasses host firewall filtering unless explicitly
  constrained.
- Migrate routine deployment away from root access when a tested replacement
  path exists.
- Current status and next actions are in `../../docs/HANDOFF.md`.
