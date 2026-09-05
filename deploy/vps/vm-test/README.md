# SHIZZLE-TEST VM harness

Production-shaped stack for the isolated VMware VPS twin
(`D:\Virtual Machines\SHIZZLE-TEST`, host-only VMnet1, `192.168.176.52` —
read `D:\Virtual Machines\AGENTS.md` before touching the VM). Same topology
as production (postgres + api + orchestrator + caddy) minus Let's Encrypt and
CloudFront: pinned self-signed TLS on :443 so Secure-cookie auth behaves
exactly as on HTTPS production, and media served from the local appdata
volume.

This harness preserves the isolated browser/relay procedure, but its local
media fixture is currently incompatible with the production library filter:
`local/` rows are hidden from `/api/library`. It cannot currently establish a
fresh end-to-end playback pass from the old fixture instructions alone. See
the fixture limitation below before relying on it as a working acceptance gate.

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
