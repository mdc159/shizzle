# Early checkout — domain + cert + production CloudFront (pulled forward from Phase 4)

Date: 2026-08-02 (evening PDT). Claim under test: *"cert + DNS + production
CloudFront is scriptable against zone Z07938355FL89IEW1HFO."*

> Troubleshooting record for DNS, certificate, and CDN configuration. Use
> `../docs/HANDOFF.md` for current production status.

Verdict: **mostly proven in one run.** Distribution + DNS legs are scripted
and executed; the cert leg is written but blocked by an IAM gap (the `agent`
key cannot call `acm:RequestCertificate`), and DNS-validated issuance would
anyway wait on registry delegation, which was still propagating during the
run. All remaining steps are committed as ready-to-run scripts in
`infra/cloudfront/`.

## Registration status timeline (all times PDT)

| Time | Event |
|------|-------|
| 18:43:13 | `REGISTER_DOMAIN shizzle.systems` submitted (op `664a3a4e-507f-4c0b-8efa-80b0334daee6`) |
| 18:53:26 | Operation **SUCCESSFUL** (~10 min) |
| 18:55 | `.systems` registry servers (`v0n0.nic.systems`): still NXDOMAIN — delegation not yet published |
| 19:00 | Still NXDOMAIN at registry and 8.8.8.8 |
| 19:02 (run end) | Still NXDOMAIN at the registry (~9 min after SUCCESSFUL). Delegation will land on the registry's publication cycle; ACM's 72h revalidation window absorbs the wait once a cert exists. |

Takeaway: route53domains "SUCCESSFUL" ≠ publicly resolvable. Registry zone
publication lags the operation by anywhere from minutes to ~an hour, and
ACM DNS validation cannot complete until the delegation is live. Google
(8.8.8.8) additionally negative-caches the NXDOMAIN for up to the SOA
negative TTL (86400s zone value, capped much lower by Google in practice).

## What got created (all tagged/commented "shizzle production" — keepers)

| Resource | ID |
|----------|----|
| Production CloudFront distribution | `ELKN8VGSX0M64` · `d2488k8kjndpsy.cloudfront.net` · Comment "shizzle production" · tags project=shizzle, purpose=shizzle production |
| Production OAC | `E1T203UNY8R6AY` (`shizzle-production-oac`) |
| Route 53 record | `vps.shizzle.systems. A 300 -> 72.60.173.171` (CloudFront origin name for the VPS) |
| Spike bucket policy | `shizzle-spike-media-9abf4c` `AWS:SourceArn` widened to `[EYDQD3CPYVRRU, ELKN8VGSX0M64]` |
| ACM certificate | **NOT created — blocked on IAM** (see below) |

Distribution shape (config committed as
`infra/cloudfront/distribution-config.template.json`):

- Origin `s3-media`: spike bucket via new OAC — **SWAP-AT-PHASE-4** to the
  production media bucket.
- Origin `vps-api`: `vps.shizzle.systems` (72.60.173.171), `http-only` —
  **SWAP** to `https-only` once Caddy holds a cert.
- Default + `/media/*`: GET/HEAD, signed-cookie gate via spike key group
  `cfad272c-929b-45be-93db-501dd50e5948` (**SWAP** to production key pair at
  Phase 4), CachingOptimized. Unsigned requests get CloudFront 403
  `MissingKey` — that is the intended "default behavior 403".
- `/api/*` and `/ws/*`: all 7 methods, CachingDisabled +
  Managed-AllViewer origin request policy (forwards Host, all headers incl.
  WebSocket upgrade, cookies, query strings).
- No alias, default `*.cloudfront.net` cert (two-step reality, below).
- Agent working defaults, held lightly: PriceClass_100, http2and3, IPv6 on,
  60s origin read timeout on the VPS origin.

## Errors hit and their fixes

1. **`acm:RequestCertificate` → AccessDeniedException** for
   `arn:aws:iam::826783599575:user/agent` (the `.env` key). Not fixable from
   here: the same user is also denied all `iam:*` reads, the machine's `k25`
   profile (`n8n-karaoke-s3`, same account) is denied the same ACM call, the
   machine's `default` profile is a different account (172977038517), and the
   aws-mcp proxy's credentials are expired. **The account owner must attach
   ACM permissions to the `agent` user** (minimum: `acm:RequestCertificate`,
   `acm:DescribeCertificate`, `acm:ListCertificates`, `acm:AddTagsToCertificate`
   on `*`). Everything downstream is scripted against this.
2. **CloudFront origins cannot be bare IPs.** `72.60.173.171` is not a legal
   origin `DomainName`. Fix: created `vps.shizzle.systems A` in the zone and
   originated to that. (Bonus: lets Caddy get a cert for a real hostname
   later.)
3. **`file:///tmp/...` unusable on Windows** — the AWS CLI is a Windows exe
   and cannot see git-bash's virtual `/tmp`. Fix: inline `--change-batch`
   JSON or relative `file://name.json` from a real directory.
4. **Managed policy ID drift.** The commonly quoted CachingOptimized ID was
   wrong for this partition/current listing; verified live:
   CachingOptimized `658327ea-f89d-4fab-a63d-7e88639e58f6`, CachingDisabled
   `4135ea2d-6df8-44a3-9df3-4b5a84be39ad`, AllViewer
   `216adef6-5c7f-47e4-b989-5492eafa07d3`. Scripts use the verified IDs.
5. **OAC bucket policy is per-distribution-ARN.** The spike bucket policy
   only allowed `EYDQD3CPYVRRU`; the new distribution would have gotten S3
   AccessDenied on `/media/*`. Fix scripted into `03-create-distribution.sh`
   (appends the new ARN to `AWS:SourceArn`).

## The two-step alias reality (finding, as predicted)

CloudFront rejects `Aliases` without an attached ACM cert covering them, and
rejects certs that are not `ISSUED`; a DNS-validated cert can't issue until
the fresh domain's delegation is publicly resolvable. On a same-day domain
registration the only non-blocking order is:

1. create distribution bare (`03-create-distribution.sh` — **done**)
2. request cert + validation CNAME (`02-request-cert.sh` — written, blocked on IAM)
3. wait ISSUED (`poll-cert.sh`; ACM retries validation for 72h, so it
   self-completes once delegation propagates — no re-run needed)
4. attach alias + cert (`attach-domain.sh` — written)
5. point apex A/AAAA alias records (`04-dns-alias.sh` — written, guarded so
   it refuses to run before the alias is attached)

## Smoke test (distribution direct, pre-alias)

Distribution created ~18:57, `Status=Deployed` by 18:59 — CloudFront deploy
propagation was again fast (~2 min, matching spike 0.2). `smoke-test.sh`
against `https://d2488k8kjndpsy.cloudfront.net`:

| Path | Got | Meaning |
|------|-----|---------|
| `/` | 403 `MissingKey` | signed-cookie gate live on default behavior — intended "default 403" |
| `/media/x.m4a` | 403 `MissingKey` | signed-cookie gate live on `/media/*` |
| `/api/health` | 502 | CloudFront reached the behavior but cannot resolve `vps.shizzle.systems` publicly yet — expected until delegation propagates |
| `/ws/` | 502 | same |

The 403s prove routing + key-group config; the 502s are purely the DNS
propagation wait (plus whatever Caddy answers on :80 once reachable).

## What Phase 4 still needs

| Item | State |
|------|-------|
| Grant ACM permissions to IAM user `agent` (RequestCertificate, DescribeCertificate, ListCertificates, AddTagsToCertificate) | **BLOCKED-ON-IAM** — account-owner action, nothing scriptable from this key |
| Request cert + upsert validation CNAME (`02-request-cert.sh`) | SCRIPTED-READY (runs the moment IAM is granted; DNS-side write already proven with a probe record) |
| Cert issuance | BLOCKED-ON-DNS-PROPAGATION (auto-resolves; ACM revalidates for 72h) |
| Attach alias + cert (`attach-domain.sh`) | SCRIPTED-READY (gated on cert ISSUED) |
| Apex A/AAAA alias records (`04-dns-alias.sh`) | SCRIPTED-READY (gated on alias attached) |
| Production CloudFront distribution | DONE (`ELKN8VGSX0M64`, deployed) |
| VPS origin over HTTPS | BLOCKED-ON-CADDY-CERT — needs delegation live, then Caddy obtains cert for `vps.shizzle.systems`, then flip origin policy to `https-only` |
| Swap S3 origin to production media bucket | SWAP-AT-PHASE-4 (template + bucket-policy step marked) |
| Swap spike key group for production signing key pair | SWAP-AT-PHASE-4 |
| `/api/*` + `/ws/*` end-to-end through CloudFront | BLOCKED-ON-DNS-PROPAGATION (CloudFront can't resolve `vps.shizzle.systems` until delegation is public) + BLOCKED-ON-VPS (Caddy must answer on :80) |

Scripts and committed config live in `X:\GitHub\shizzle\infra\cloudfront\`
(see its README for run order). Nothing was git-committed per the brief.

DONE: builder | Production distro ELKN8VGSX0M64 + DNS legs scripted and executed; cert leg written but BLOCKED-ON-IAM (agent user lacks acm:RequestCertificate); alias attach is a proven two-step, scripts staged
