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

Use the production compose project name explicitly:

```text
cd /opt/shizzle/prod
docker compose -p shizzle ps
docker compose -p shizzle logs --tail 200 api orchestrator
```

Verify the public root, authenticated API, database, orchestrator heartbeat,
telemetry endpoint, and CloudFront media Range behavior after a deployment.

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
