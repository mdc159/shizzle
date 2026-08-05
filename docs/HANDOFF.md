# Shizzle — Handoff Brief

Last updated: 2026-08-04. Repo: `X:\GitHub\shizzle` (git, branch `master`).
Read this first, then `docs/superpowers/specs/2026-08-02-shizzle-cloud-karaoke-design.md` (design authority) and `specs/shizzle-cloud-karaoke-implementation.html` (phased plan). Spike evidence is in `spikes/RESULTS*.md`. Salvage provenance is in `docs/provenance.md`.

---

## 1. What this is

A cloud karaoke **stem mixer**. Paste a song → six instrument stems (vocals, drums, bass, guitar, piano, "shizzle"/other) split on a GPU → stored on S3, served over CloudFront CDN → any browser plays the video muted while six `<audio>` streams feed per-stem faders. An iPad/phone can pair by QR and become a touch mixing surface for a player on a big screen. Private tool for Mike + a few musician friends. Successor to the three `k25*` repos (their good parts were copied in, not their git history).

**Live now: https://shizzle.systems** — passcode `shizzle-stems-802`. 27-track library, full video + 6-stem playback with faders, all-cloud, iPad-verified.

---

## 2. Current state (what's live and working)

- **Frontend + API + orchestrator + Postgres** deployed on the Hostinger VPS `72.60.173.171` (`srv1875370`, KVM 4, 4 vCPU/16 GB) at `/opt/shizzle/prod/`, fronted by **Caddy** with automatic Let's Encrypt on `shizzle.systems`. Health: `https://shizzle.systems/api/health` → api/db/orchestrator green.
- **Media delivery: CloudFront CDN** (distribution `ELKN8VGSX0M64` / `d2488k8kjndpsy.cloudfront.net`) fronting the private S3 bucket `karaoke-pimpshizzle` via OAC, gated by **signed cookies** the API issues after the passcode. Caddy reverse-proxies `/cdn/*` same-origin to the distribution so cookies ride along (no cross-origin, no ACM cert needed for media).
- **27-track library** imported from the legacy bucket (server-side S3 copy). Playback proven end-to-end (Playwright, real Chrome + iPad viewport): stems stream via Range, sample-locked to the video clock, sub-10 ms inter-stem skew.
- **The GPU split pipeline is proven correct** — the worker ran a full split end-to-end on an RTX 4070 (real S3 in/out, both integrity gates pass, 43 s). See §5.

## 3. What's NOT working / open

1. **RunPod GPU splitting does not complete on RunPod's hardware.** This is the #1 blocker. The worker image is correct (proven locally), but RunPod workers wedge — see §5 for the full trail and the leading fix (bake demucs weights into the image).
2. **New-song ingest is off in production.** The VPS orchestrator runs a lightweight (no-GPU) pipeline; adding a song from the UI won't process until RunPod works. The 27 tracks are the imported legacy library.
3. **Remote QR pairing (iPad-as-controller) is built but not merged/deployed.** Complete on branch `worktree-agent-ad70220c6abba6c9e` (99 tests pass). Overlaps `App.tsx`/`main.tsx`/`settings.py`/`main.py` with deployed code — needs a supervised merge + redeploy.
4. **4 lost AC/DC tracks** (For Those About To Rock, Highway to Hell, 2× Dirty Deeds) — legacy stems missing; re-split script built (`scripts/resplit_track.py`, branch `worktree-agent-a4fb3566312ab2944`), jobs staged, blocked on RunPod (§5).
5. **YouTube URL ingest** unsolved for PC-off use — datacenter IP is bot-blocked even with PO-tokens/cookies (§4). Uploads work; a residential proxy (~$1–5/mo) or a home-helper is the path.
6. Passcode auth is launch-minimal (no rate limit / per-device revoke). No Postgres backups yet. `shizzle.systems` on CloudFront-as-app-origin is blocked (no ACM permission on any available IAM key) — but unnecessary; Caddy serves the app fine.

---

## 4. Problems we hit, and how each resolved (the important history)

| Problem | Resolution |
|---|---|
| Old app "heavy, froze, one song max" | Root cause was decoding whole files into RAM. New engine streams six compressed AAC via Range. |
| AWS CLI silently hit Cloudflare R2 | Machine-level `AWS_ENDPOINT_URL` env var redirects all AWS calls. **Always `unset AWS_ENDPOINT_URL`** before AWS commands. |
| "ACM cert blocked on IAM" (claimed a blocker) | It was never needed — Caddy auto-TLS serves the app; CloudFront serves media over its own hostname. Don't chase the ACM cert; all 3 IAM users lack ACM+IAM perms (verified). |
| Domain HTTPS | `shizzle.systems` registered in AWS Route 53 (zone `Z07938355FL89IEW1HFO`), apex A → VPS, Caddy issues the cert automatically. |
| YouTube from the VPS | Hostinger datacenter IP is hard bot-blocked (bare, PO-token, and cookies all failed). Uploads unaffected. |
| Demucs `--clip-mode rescale` detuned drums −1.24 dB | Fixed: float output + one common gain, recorded in manifest. Integrity gates verify it. |
| Decoded AAC clips at unity (+1 dBFS) | Master bus has a limiter + −3 dB headroom (hard requirement, not polish). |
| **Playback froze again (2026-08-04)** | Two bugs: (a) one track ("The Pot") imported as 573 MB WAV instead of M4A; (b) engine waited for `canplaythrough` (full buffer). Both fixed: WAV→M4A transcoded, engine now gates on `canplay` (streams). Guard added so non-M4A can't enter the library. Verified with real playback. |
| Model flips Fable 5 ↔ Opus 4.8 | Not a project bug — Max-plan Fable-5 usage cap + a stale local usage-credits cache. Fix: restart Claude Code (the app instructs this) once usage credits are enabled. |

**Standing lesson (Mike's repeated correction):** never report an implementation-path obstacle as a project blocker until alternative paths against the *actual goal* are exhausted, and check Mike's existing infrastructure first (his cbass.space service farm = DNS→VPS→Caddy auto-TLS pattern). A claimed "blocker" repeatedly dissolved the moment his own architecture was examined.

---

## 5. RunPod — the one real blocker, full trail (start here)

The worker code and image are **correct** — do not rewrite them. The problem is RunPod-side.

**Proven:** the modernized worker (`ghcr.io/mdc159/shizzle-worker:v2`, built by GitHub Actions from repo `mdc159/shizzle-worker`) ran a **complete split on an RTX 4070** in serverless mode with real S3 I/O — download → demucs (15 s) → both integrity gates pass (gate_a 23.6 dB, gate_b 66 dB) → 6 stems + video + manifest uploaded to S3, 43 s total. Reproduce: `docker run --rm --gpus all -e AWS_* ghcr.io/mdc159/shizzle-worker:v2 sh -c 'echo <input.json> > /app/test_input.json && python3 handler.py'`.

**What was tried on RunPod, in order:**
1. Old endpoint (year-old template) — template was *deleted* (404) + carried a dead AWS key → every job failed. Abandoned.
2. Created a fresh endpoint. Workers went `unhealthy` and wedged; jobs never completed.
3. Hypothesised the Docker `HEALTHCHECK` (start-period 5 s too short for the 15 GB image warmup) → RunPod cycles workers. Removed it (repo Dockerfile, CI rebuilt `:v2`).
4. Recreated the endpoint on the fixed image → **`unhealthy` count dropped to 0** (healthcheck WAS one real cause) — but jobs *still* don't complete; a "running" worker never finishes.
5. Batch of 4 re-split jobs → workers flipped `ready=3 → unhealthy=3` again under load.

**Leading unfixed hypothesis (strong, not yet tested):** the demucs `htdemucs_6s` weights are **not baked into the image** (the Dockerfile says so — they download to `/root/.cache/torch` on first run). Each cold RunPod worker re-downloads ~250 MB of weights and hangs long enough that RunPod's health probe times out → wedged. **This matches every symptom.**

**Current endpoint:** id `tevdw8022hs8hn` (in `.env` as `RUNPOD_ENDPOINT_ID`), one RunPod account, key valid. There is also a stale endpoint `r370i6ad7h75m3` to ignore/delete.

---

## 6. Next plan (do this, in order)

1. **Fix RunPod (unblocks everything downstream).** Bake the demucs weights into the worker image: in `worker/Dockerfile`, after installing demucs, add `RUN python3 -c "from demucs.pretrained import get_model; get_model('htdemucs_6s')"` so the weights are in the image, not downloaded per cold-start. Push (via the `mdc159/shizzle-worker` CI — do NOT try to `docker push` from this PC, the token lacks `write:packages`; the CI's `GITHUB_TOKEN` has it). Point the template at the new tag, submit the golden-fixture proof job (`fixtures/golden-30s.mp4` staged at `tracks/proofc538/1/staging/source.mp4`), and confirm it COMPLETES with output in S3. **Verify workers stay `healthy` and a job reaches `COMPLETED` before declaring victory.** If it still wedges, get the actual RunPod worker stderr (console → endpoint → worker → Logs) — that's the one piece of evidence never obtained.
2. **Once RunPod completes a job:** run `scripts/resplit_track.py --all-lost` to finish the 4 lost AC/DC tracks (jobs already staged), and wire the orchestrator's `dispatched`/`splitting` stages to the RunPod client + webhook so new-song ingest works from the UI.
3. **Merge + redeploy the remote QR surface** (branch `worktree-agent-ad70220c6abba6c9e`). Resolve the `App.tsx`/`main.tsx`/`settings.py`/`main.py` conflicts, rebuild UI, redeploy to `/opt/shizzle/prod` (preserve the `caddy_data` cert volume). This makes iPad-as-controller live.
4. **YouTube ingest:** add a residential proxy for yt-dlp on the VPS (cheapest reliable path), or a tiny home-helper downloader. Uploads already work.
5. **Harden for real use:** passcode rate-limit + per-device token revocation; nightly Postgres dump to S3; disable the legacy ungated CloudFront distro `E1TTUZICNONOHR` (it serves the library publicly).

---

## 7. Access & gotchas for the next agent

- **Credentials** live in `X:\GitHub\shizzle\.env` (gitignored). AWS (IAM user `agent`, account 826783599575), RunPod, Hostinger token, Google OAuth. `secrets/` holds keypairs/cookies (gitignored).
- **Always `unset AWS_ENDPOINT_URL`** before AWS CLI (machine R2 redirect).
- **Compose gotcha:** a machine env var `COMPOSE_PROJECT_NAME=gbrain-dev` hijacks unqualified `docker compose` — always pass `-p shizzle`.
- **VPS:** `ssh root@72.60.173.171` (main key `~/.ssh/id_ed25519`). Prod stack at `/opt/shizzle/prod/` (`docker compose -p shizzle`). Its `.env` holds the runtime secrets.
- **PowerShell 5.1 pitfall:** writing files with `Set-Content` mangles UTF-8 (mojibake in the HTML plan happened twice) — use the Write tool or `-Encoding utf8` explicitly.
- **Do NOT push Docker images from this PC** (residential upload + no `write:packages` token). Build via the `mdc159/shizzle-worker` GitHub Actions CI, or on the VPS for local use.
- **Security note:** subagents were given latitude to mutate production (root SSH, S3 deletes, prod deploys) — one was flagged for a "mass delete" that turned out to be redundant WAV copies (source intact in `karaoke/pub/the-pot-2d88b7a5/` and `karaoke/out/2d88b7a5-.../`). Consider requiring subagents to confirm destructive prod actions before executing.
