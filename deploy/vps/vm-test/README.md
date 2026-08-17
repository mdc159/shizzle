# SHIZZLE-TEST VM harness

Production-shaped stack for the isolated VMware VPS twin
(`D:\Virtual Machines\SHIZZLE-TEST`, host-only VMnet1, `192.168.176.52` —
read `D:\Virtual Machines\AGENTS.md` before touching the VM). Same topology
as production (postgres + api + orchestrator + caddy) minus Let's Encrypt and
CloudFront: pinned self-signed TLS on :443 so Secure-cookie auth behaves
exactly as on HTTPS production, and media served from the local appdata
volume.

The VM has no internet by design. Everything is ferried over the host-only
network:

```text
# on the host
docker build -f library/Dockerfile.api -t shizzle-api:vm-test library/
docker save shizzle-api:vm-test -o shizzle-api-vm-test.tar
npm --prefix player run build && tar -czf player-dist.tgz -C player/dist .
scp *.tar player-dist.tgz compose.vm.yml Caddyfile .env mike@192.168.176.52:~/shizzle-test/

# in the VM (~/shizzle-test)
tar -xzf player-dist.tgz -C player
sudo docker load -i shizzle-api-vm-test.tar
mkdir -p certs && openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 \
  -keyout certs/key.pem -out certs/cert.pem -days 30 -nodes \
  -subj '/CN=192.168.176.52' -addext 'subjectAltName=IP:192.168.176.52'
sudo docker compose -p shizzle -f compose.vm.yml up -d postgres
sudo docker compose -p shizzle -f compose.vm.yml run --rm --no-deps api alembic upgrade head
sudo docker compose -p shizzle -f compose.vm.yml up -d
```

`.env` (test-only values, never production secrets): `SHIZZLE_API_IMAGE`,
`SHIZZLE_PASSCODE`, `AUTH_VERSION`, `TOKEN_SIGNING_SECRET`, `POSTGRES_USER`,
`POSTGRES_PASSWORD`, `POSTGRES_DB`.

A real track becomes a local-profile track by copying a generation into the
appdata volume and inserting one row:

```text
sudo docker cp gen shizzle-api:/app/data/<dir>
sudo docker exec shizzle-api mv /app/data/<dir>/manifest.json /app/data/<dir>/stems.json
# INSERT INTO tracks (id, title, artist, duration_seconds, s3_prefix,
#   generation, manifest_key, created_at)
# VALUES (<uuid>, <title>, <artist>, <seconds>, 'local/<dir>', 1,
#   'local/<dir>/stems.json', now());
```

Then from the host, the full physical test (two real browsers, real relay,
track actually playing, mute verified in measured post-gain PCM):

```text
cd player && SHIZZLE_E2E_BASE_URL=https://192.168.176.52 \
  SHIZZLE_E2E_PASSCODE=<passcode> SHIZZLE_E2E_HEADLESS=1 \
  npx playwright test e2e/remote-mixer-vm.spec.ts
```

First run of this harness (2026-08-17) caught a real bug: the player's video
staging fetch used `credentials: 'omit'`, so neither the device-token cookie
(local profile) nor the /cdn signed cookies could ride — fixed to
`'same-origin'`.
