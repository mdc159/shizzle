# RESULTS — Phase 4 frontend deploy (iPad-ready Shizzle on shizzle.systems)

Date: 2026-08-02 (VPS timestamps 2026-08-03 UTC). Builder pass.

**Verdict: DONE — media plays on iPad.** https://shizzle.systems serves the real
Shizzle app; a device enters the passcode, opens the 27-track library, picks a
track, and the player streams the video + all six stems through CloudFront
(signed cookies, Range) with the stem faders live. Verified outside-in from the
Windows box (real HTTP + a headless iPad-viewport browser), zero console errors.

## URL + passcode

| | |
|---|---|
| URL | **https://shizzle.systems** |
| Passcode | **`shizzle-stems-802`** (Mike types this once per device; rotate any time — see below) |

Rotating the passcode: edit `SHIZZLE_PASSCODE` in `/opt/shizzle/prod/.env` and
`docker compose -p shizzle -f compose.prod.yml up -d api orchestrator`. The
passcode is bound into every device token's signature, so a rotation revokes
all existing tokens automatically (design §7).

## What is deployed

VPS `root@72.60.173.171` (Hostinger KVM), `/opt/shizzle/prod/`, compose project
`shizzle` (`compose.prod.yml`):

- **caddy** — serves the built UI (`ui/dist`) at shizzle.systems with automatic
  HTTPS (the existing Let's Encrypt cert was preserved: `caddy_data` is reused
  as the external `shizzle-probe_caddy_data` volume). Reverse-proxies `/api` +
  `/ws` to the api container, and same-origin `/cdn/*` to the CloudFront
  distribution (Host-rewritten).
- **api** — FastAPI control plane. Ran `alembic upgrade head` on start
  (`0001_initial`), then uvicorn. Passcode gate + CloudFront cookie issuing +
  cloud-media manifest route.
- **orchestrator** — durable loop, heartbeating (`/api/health` reads it). Runs
  the `test` pipeline (no Demucs in this image; no new ingest at launch).
- **postgres** — named volume `shizzle_pgdata`, not published to the host.
- The probe stack (`shizzle-probe`) was brought **down** (volumes kept) to free
  80/443; its cert volume is reused so no re-issuance.

Code committed on `master` (`10d62e7`): passcode auth (`auth.py`), CloudFront
signed cookies (`cloudfront.py`, lifted from spike 0.2), cloud-media manifest
route (`media.py`, routes), UI passcode gate + bearer-token `authFetch` +
same-origin `/cdn` media wiring, `infra/vps/compose.prod.yml` + `Caddyfile.prod`.
Server: 93 pytest pass, ruff clean, `routes.py` mypy at pre-existing baseline,
new modules mypy-clean. UI: eslint/tsc/build/knip clean.

## Media delivery — CloudFront signed cookies (the designed path)

Media is the private S3 bucket `karaoke-pimpshizzle` (`tracks/{id}/1/…`) fronted
by the **production CloudFront distribution `ELKN8VGSX0M64`
(`d2488k8kjndpsy.cloudfront.net`)** via OAC `E1T203UNY8R6AY`, default behavior
gated by trusted key group `cfad272c-…` (public key `KRNC9VLVC15DN`). Confirmed
live: the distribution's `s3-media` origin already points at
`karaoke-pimpshizzle` — no origin swap was needed this pass.

How the cookies reach the edge without a media subdomain (the ACM/alias for
`shizzle.systems` on CloudFront is still **blocked-on-IAM**, per
RESULTS-domain-cdn.md): the app issues the three `CloudFront-*` cookies scoped
to `Path=/cdn` on shizzle.systems, and **Caddy reverse-proxies `/cdn/*`
same-origin to the distribution**, forwarding the cookies. The player fetches
`/cdn/tracks/{id}/1/…` same-origin (cookies ride along), Caddy strips `/cdn` and
forwards to CloudFront `/tracks/…`, which enforces the signed-cookie gate over
the private bucket. This keeps signed cookies as the delivery mechanism (NOT
presigned URLs) while sidestepping the blocked cert. Signing key
(`secrets/cloudfront-spike/private_key.pem`) is mounted into the api container
at `/run/secrets/cloudfront_private_key.pem` (never committed).

## Verification (all from OUTSIDE — the Windows box as the iPad)

Real HTTP:

| Check | Result |
|---|---|
| `GET https://shizzle.systems/` | 200, real UI HTML (`<title>karaoke-ui</title>`, `/assets/index-*.js`, `#root`) — **not** the "shizzle vps ok" placeholder |
| `GET /api/health` | `{"status":"ok","api":true,"db":true,"orchestratorAlive":true}` |
| `GET /api/library` unauthenticated | **401** (gate on) |
| `POST /api/auth` wrong passcode | **401** |
| `POST /api/auth` correct passcode | 200, token + `mediaCookies:true`; sets `shizzle_token` + `CloudFront-Policy/-Signature/-Key-Pair-Id` |
| `GET /api/library` with token | **27 tracks**, titles + durations match manifests |
| `GET /api/tracks/{id}/manifest` | manifest with `video` + 6 `stems[].file` as `/cdn/tracks/{id}/1/…` |
| `GET /cdn/.../video.mp4` with cookies, `Range: 0-1023` | **206**, `Content-Range: bytes 0-1023/31529427`, `video/mp4`, `Via … cloudfront` |
| `GET /cdn/.../video.mp4` **without** cookies | **403** (signed-cookie gate) |
| `GET /cdn/.../stems/vocals.m4a` with cookies | 206 Range; full download 3,170,473 B, valid `ftypM4A` container |

Headless browser (Playwright chromium, **iPad-ish viewport 820×1180 @2×,
touch**; deviation: chromium not the `chrome` channel):

- Loaded the site → passcode gate → entered `shizzle-stems-802` → authenticated.
- Opened library: **27 tracks** visible; selected "The Pot" (Tool).
- Player mounted; `<video>` src = `/cdn/tracks/f995371a…/1/video.mp4`.
- All **6 stems** fetched over `/cdn` (206) + video.mp4 (206); distinct stems
  loaded OK = 6.
- Pressed play: `video.currentTime` advanced to **4.05 s**, `paused:false`,
  `readyState:4`, duration 378.6 s — **real playback**.
- Mixer drawer: six stem faders (Vocals/Drums/Bass/Guitar/Piano/Shizzle) each
  with Solo/Mute + dB (8 sliders incl. master/seek).
- **console errors: 0, page errors: 0.**

Screenshots:
- `spikes/frontend-deploy-ipad.png` — the video playing full-screen (1640×2360).
- `spikes/frontend-deploy-mixer.png` — the six stem faders.

## What does NOT work yet (honest gaps)

- **Remote QR pairing (design §6): NOT wired.** No `/ws` handler or pairing/
  session endpoints exist. Caddy proxies `/ws` to the api (ready), but the
  server has no WebSocket route — the iPad-as-touch-surface / projector split is
  future work. iPad plays *directly* today (library + player + faders), which is
  the launch goal.
- **New-track ingest is off.** The orchestrator runs the `test` pipeline (this
  slim image has no Demucs; yt-dlp ingest is Phase 3 code not exercised here).
  The library is the 27 imported legacy tracks. Adding songs from the UI will
  not process yet.
- **9 of 36 legacy folders were skipped as degraded** (their manifests
  reference stems absent from the bucket — a pre-existing data finding, see
  RESULTS-legacy-import.md). 27 imported, matching the earlier local import.
- **Passcode gate is launch-minimal.** No rate limit on `/api/auth` and no
  per-device revocation list yet (design §7 wants both). Rotation-revokes-all
  works; individual device revocation does not.
- **CloudFront on the app's own hostname** (ACM alias for shizzle.systems) is
  still blocked-on-IAM. The same-origin `/cdn` Caddy proxy is the working
  substitute; if the account owner later grants ACM, the app could move fully
  behind CloudFront, but it is not required for iPad playback.
- **Signing keypair is the spike keypair reused** (`KRNC9VLVC15DN`), since the
  production distribution already trusts that key group. Fine for launch;
  rotating to a dedicated production keypair is a clean-up follow-up.
- **Ops**: no pg backups/monitoring on the box yet (VPS punch-list items 4/8
  from RESULTS-vps.md remain).

## Untouched (per brief)

RunPod, `worker/`, `spikes/RESULTS-runpod*` — not touched. The legacy import was
idempotent and copied nothing (media already in S3; only DB rows written).

DONE: builder | shizzle.systems live + iPad-ready — passcode gate, 27-track library, full video+6-stem playback via CloudFront signed cookies (206/Range), stem faders; verified outside-in + headless iPad viewport, 0 errors. Gaps: remote QR pairing + new-track ingest not wired.
