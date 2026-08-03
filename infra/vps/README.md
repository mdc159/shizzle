# infra/vps — production VPS bootstrap

Target: Hostinger KVM 4, `srv1875370`, Ubuntu 24.04 LTS, 4 vCPU / 16 GB / 200 GB,
IP `72.60.173.171`. Checked out early (2026-08-02) ahead of Phase 4 — see
`spikes/RESULTS-vps.md` for the baseline, timings, and the punch list.

Launch architecture (Mike's decision, 2026-08-02): **DNS -> VPS -> Caddy with
automatic Let's Encrypt** — same shape as the existing cbass.space service
farm. NOT CloudFront aliases/ACM. Route 53 holds `shizzle.systems A ->
72.60.173.171` and `www CNAME -> shizzle.systems`.

## Files

| file | purpose |
|---|---|
| `setup.sh` | Idempotent root bootstrap: apt upgrade, unattended-upgrades, UTC, fail2ban (sshd), Docker Engine + compose plugin from the official Docker repo, ufw (22/80/443), deploy user `shizzle`, `/opt/shizzle/` |
| `probe/compose.yml` | Minimal probe stack: postgres:16-alpine (named volume, healthcheck, not published) + caddy:2 on 80/443 |
| `probe/Caddyfile` | `:80` catch-all (IP probe) + `shizzle.systems, www.shizzle.systems` with automatic HTTPS, both answering `shizzle vps ok` |
| `probe/.env.example` | Template for the probe's `POSTGRES_PASSWORD` |

## Bootstrap (from this repo, Windows or anywhere with ssh)

```sh
ssh root@72.60.173.171 'bash -s' < infra/vps/setup.sh
```

Re-running is safe — every step checks before it changes. After the first run,
open a SECOND ssh session to confirm ufw didn't cut you off before trusting it.

## Probe stack

```sh
scp infra/vps/probe/compose.yml infra/vps/probe/Caddyfile root@72.60.173.171:/opt/shizzle/
ssh root@72.60.173.171 'cd /opt/shizzle && { [ -f .env ] || echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)" > .env; }; chmod 600 .env; docker compose up -d'
curl http://72.60.173.171/          # IP probe through ufw  -> shizzle vps ok
curl -sI https://shizzle.systems    # TLS proof             -> HTTP/1.1 200, Server: Caddy
```

TLS: Caddy self-obtains and renews Let's Encrypt certs (TLS-ALPN-01 on :443).
Certs/account live in the `caddy_data` volume — keep that volume across
recreates or issuance starts over (and rate limits are real).

## Ops notes

- **Root login stays enabled** until deploys migrate to the `shizzle` user
  (has docker group + same key); then set `PermitRootLogin no` in
  `/etc/ssh/sshd_config` and restart sshd (marked TODO in setup.sh).
- **Docker published ports bypass ufw** (Docker writes its own iptables NAT
  rules). The probe's postgres publishes nothing, so it is unreachable from
  outside; keep that pattern in the real stack — only Caddy publishes 80/443.
- The probe's `:80` catch-all suppresses Caddy's automatic HTTP->HTTPS
  redirect for the domain (explicit :80 server wins). The real stack should
  drop the catch-all so the standard redirect comes back.
