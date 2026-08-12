# RESULTS — VPS early checkout (pulled forward from Phase 4)

Date: 2026-08-02 (all server timestamps 2026-08-03 UTC — box is UTC, local evening).
> Troubleshooting record for VPS reachability, TLS, compose, and service health.
> Use `../../docs/HANDOFF.md` for current status.

Box: Hostinger KVM 4, `srv1875370`, `72.60.173.171`. Executed from the Windows dev
machine over SSH (root, default ed25519 key). Everything scripted in `deploy/vps/`.

**Verdict: no stumbling blocks.** Bare box to full outside-in HTTPS proof in ~20
minutes wall clock, ~80 s of actual bootstrap runtime. Zero errors, zero retries.

## Architecture note (changed mid-spike)

Original brief assumed CloudFront in front with origin TLS deferred. Mike's
decision (relayed by the orchestrator during the spike): launch path is
**DNS -> VPS -> Caddy with automatic Let's Encrypt**, like the cbass.space
farm — no CloudFront aliases/ACM. Route 53 was upserted during the spike
(`shizzle.systems A -> 72.60.173.171`, `www CNAME -> shizzle.systems`). The
probe was extended to cover the domain with auto-HTTPS, and the cert **already
issued** — see below.

## Baseline (before any changes)

- Ubuntu 24.04.4 LTS (noble), kernel 6.8.0-134-generic x86_64
- Disk: 200 GB (`/dev/sda1` 193 GB usable, 797 MB used, 1%)
- RAM: 15 Gi total, ~397 Mi used, **no swap configured**
- CPU: 4 vCPU
- Listening: sshd on 22 (+ systemd-resolved on loopback). Nothing else.
- Pre-installed junk: none. No docker, no snap output, no panels
  (hestia/cpanel/plesk absent). Only stock services + qemu-guest-agent.
  Clean image — nothing to fight.

## What was installed (versions)

Via `deploy/vps/setup.sh` (idempotent, re-runnable, `ssh root@... 'bash -s' < setup.sh`):

| thing | version / state |
|---|---|
| apt upgrade | 47 packages upgraded (openssh, libc6, openssl 3.0.13-0ubuntu3.12, ...); `linux-image-virtual` kept back (needs reboot cycle, deferred) |
| timezone | Etc/UTC (was already UTC) |
| unattended-upgrades | 2.9.1, enabled via `20auto-upgrades` (update-lists + upgrade daily) |
| fail2ban | 1.0.2, sshd jail active (systemd backend, 1h ban / 5 retries), 0 bans so far |
| Docker Engine | 29.7.1 (docker-ce from download.docker.com apt repo, NOT Ubuntu's) |
| Docker Compose plugin | v5.3.1; containerd.io 2.2.6, buildx 0.36.0 |
| hello-world | ran clean ("installation appears to be working correctly") |
| ufw | active + on-boot; allow 22/tcp, 80/tcp, 443/tcp (v4+v6); default deny incoming |
| deploy user | `shizzle` uid=1000, groups docker(988); root's authorized_key copied; SSH as `shizzle@` verified, can run `docker ps` |
| /opt/shizzle | owned shizzle:shizzle, holds the probe stack |

SSH survival: fresh second session opened AFTER `ufw --force enable` — connected
fine (firewall entries added before enable; never locked out).

## Probe stack (LEFT RUNNING — this is the liveness proof)

`/opt/shizzle/`: `compose.yml` + `Caddyfile` + `.env` (generated
`POSTGRES_PASSWORD=$(openssl rand -hex 24)`, chmod 600, probe-only credential).
Source of truth in repo: `deploy/vps/probe/`.

- `db`: postgres:16-alpine, named volume `pgdata`, healthcheck `pg_isready` —
  **healthy**, `accepting connections`. Deliberately NOT published to the host
  (and note: Docker-published ports bypass ufw, so "don't publish" is the real
  firewall for db).
- `caddy`: caddy:2, ports 80/443 (+443/udp for h3), volumes `caddy_data`
  (ACME certs/account — must persist) + `caddy_config`.

## Outside-in proof (from the Windows dev machine, through ufw)

```
curl http://72.60.173.171/         -> "shizzle vps ok", HTTP 200 in 0.159s
curl -sI https://shizzle.systems   -> HTTP/1.1 200 OK, Server: Caddy, Alt-Svc: h3
curl https://shizzle.systems/      -> "shizzle vps ok"
curl https://www.shizzle.systems/  -> "shizzle vps ok"
```

## TLS: cert ALREADY ISSUED (propagation beat the forecast)

The registry NS publication (NXDOMAIN at ~19:02) was expected to lag
minutes-to-hours; by 02:14 UTC public DNS (local resolver AND 8.8.8.8) resolved
`shizzle.systems -> 72.60.173.171`, and Caddy's ACME retry loop had already
converged. Both `shizzle.systems` and `www.shizzle.systems` obtained real
Let's Encrypt certs via **TLS-ALPN-01** on :443, first try in the log, no
errors:

- issuer `C=US, O=Let's Encrypt, CN=YE1`
- `CN=shizzle.systems`, notBefore Aug 3 01:15:33 2026, notAfter Nov 1 01:15:32 2026
- Renewal handled by Caddy (ARI info fetched; certs in `caddy_data` volume)

Later verification (should stay true): `curl -sI https://shizzle.systems`
-> `HTTP/1.1 200`, `Server: Caddy`. If it ever regresses, read
`docker compose logs caddy` in `/opt/shizzle` for the ACME error.

Note for the record (pre-decision tradeoff, now moot): with CloudFront in
front, origin TLS would have been a choice between CF->origin over HTTP :80
(simple, unencrypted hop, origin naked on its IP) vs an origin cert. The
direct DNS->Caddy path makes Caddy the single TLS owner — simpler, matches
cbass.space ops.

## Timings

| step | wall clock |
|---|---|
| baseline capture | ~10 s |
| setup.sh total (apt update+upgrade 47 pkgs, fail2ban, Docker install, ufw, user) | **77 s** (01:55:19 -> 01:56:36 UTC) |
| probe stack pull+up (postgres+caddy images) | ~25 s |
| ACME issuance (both hostnames) | <5 s once DNS resolved |
| outside-in curl (HTTP) | 0.159 s |

Datacenter bandwidth is excellent: apt at 7–17 MB/s, Docker's 100 MB of debs in 1 s.

## Errors hit

None. Only cosmetic noise: fail2ban postinst SyntaxWarnings (upstream test
files, harmless), needrestart deferring getty/logind restarts (normal), and
`linux-image-virtual` kept back (kernel update wants a reboot — schedule one
before launch, it also picks up the libc/openssl upgrades for pid 1).

## Punch list for the real deploy (Phase 2/4)

1. **Real compose stack** replaces the probe at `/opt/shizzle/` — server,
   worker, postgres, caddy. Keep: named volumes, healthchecks, db unpublished,
   only caddy on 80/443. Drop the Caddyfile's `:80` catch-all so Caddy's
   automatic HTTP->HTTPS redirect returns (the explicit `:80` server currently
   suppresses it — plain http on the domain answers 200 instead of redirecting).
   Replace `respond` with `reverse_proxy` to the app service.
2. **Preserve `caddy_data` volume** through all redeploys — it holds the LE
   account + certs; losing it risks rate limits.
3. **Secrets transfer**: `.env` currently hand-generated on the box. Decide the
   method (scp from `secrets/` at deploy time vs sops/age vs 1Password CLI) and
   script it. `.env` must never enter git.
4. **Backups**: nothing yet. Need pg_dump cron (or wal-g) + offsite target
   (S3/B2), and a documented restore drill. The `pgdata` volume is the only
   state on the box besides certs.
5. **Deploy user migration**: switch deploys to `shizzle@` (verified working,
   docker group), then set `PermitRootLogin no` + restart sshd. Not enforced
   yet by design.
6. **Reboot before launch** to activate the held-back kernel and restart
   deferred services (getty/logind still on old libs).
7. **Swap**: none configured. 16 GB is plenty for the stack, but a 2–4 GB
   swapfile is cheap OOM insurance for postgres+worker spikes.
8. **Monitoring**: nothing on the box watches the stack. Minimum: uptime check
   against https://shizzle.systems + `docker compose ps` health in whatever
   ops channel Phase 4 picks.
9. **fail2ban scope**: only sshd jailed. If anything beyond Caddy ever
   publishes a port, add a jail for it.
10. **DNS**: A/www records live in Route 53 (set by orchestrator during this
    spike). Any future IP change must update Route 53 first — certs follow
    automatically, but only after DNS moves.

DONE: builder | VPS bootstrapped (Docker 29.7.1, ufw, fail2ban, deploy user) + probe stack live; outside-in HTTP AND https://shizzle.systems with real LE cert both proven, zero errors
