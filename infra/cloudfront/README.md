# infra/cloudfront — shizzle production public-entry path

Scripts that stand up the production domain + CDN entry path:
`shizzle.systems` -> CloudFront -> {S3 media (signed cookies), VPS API/WS}.

All scripts are bash, load credentials from the repo `.env` via `env.sh`
(which also clears the machine-level `AWS_ENDPOINT_URL` R2 override), and
never print secrets. Region is pinned to `us-east-1` (CloudFront, ACM-for-
CloudFront, and route53domains all require it).

## Run order

| # | Script | What | Needs |
|---|--------|------|-------|
| 1 | `01-check-registration.sh` | Domain registration status + public NS check | read-only |
| 2 | `02-request-cert.sh` | ACM cert (apex + wildcard, DNS validation) + upsert validation CNAME into zone | `acm:RequestCertificate` (**currently denied for the `agent` IAM user**) |
| 3 | `03-create-distribution.sh` | Production distribution (no alias yet), OAC, vps origin DNS name, spike-bucket policy widening | done — `ELKN8VGSX0M64` |
| — | `poll-cert.sh` | Watch cert status | cert-arn.txt |
| 4 | `attach-domain.sh` | Attach `shizzle.systems` alias + issued cert to the distribution | cert ISSUED |
| 5 | `04-dns-alias.sh` | Apex alias A/AAAA -> distribution | alias attached |

State files (written by the scripts, committed for traceability):
`cert-arn.txt`, `distribution-id.txt`, `distribution-domain.txt`.

## Why alias attachment is a separate step

CloudFront refuses `Aliases` unless the distribution also carries an ACM cert
covering them, and refuses an ACM cert that is not `ISSUED`. DNS-validated
certs cannot issue until the domain resolves publicly, which lags domain
registration (registry zone publication). So on a fresh domain the only
non-blocking order is: create distribution bare -> cert issues -> attach
alias+cert -> point DNS.

## SWAP-AT-PHASE-4 markers

- **Origin `s3-media`** points at the spike bucket
  `shizzle-spike-media-9abf4c`. Swap `DomainName` in the distribution config
  and move the bucket policy statement to the production media bucket.
- **Origin `vps-api`** (`vps.shizzle.systems` -> 72.60.173.171) is
  `http-only` because Caddy on the VPS has no cert yet. Once it does, swap
  `OriginProtocolPolicy` to `https-only`.
- **Key group** on default + `/media/*` behaviors is the spike key group
  `cfad272c-929b-45be-93db-501dd50e5948`. Swap to a production key pair at
  Phase 4 (generate new RSA pair, upload public key, new key group, update
  behaviors, rotate the signing key on the server).

## Gotchas learned the hard way

- CloudFront origins must be DNS names — a bare IP (`72.60.173.171`) is
  rejected, hence the `vps.shizzle.systems` A record.
- `aws` CLI on Windows cannot read `file:///tmp/...` (git-bash virtual path);
  use relative `file://name.json` from a real directory, or inline JSON.
- Managed cache policy IDs: verify with `list-cache-policies --type managed`;
  CachingOptimized here is `658327ea-f89d-4fab-a63d-7e88639e58f6`.
- OAC bucket policies are per-distribution-ARN: every distribution reading
  the bucket must be listed in `AWS:SourceArn` (03 handles this).
