# Spike 0.2 results — CloudFront signed cookies over private S3 (OAC)

Date: 2026-08-02 · Region: us-east-1 · Account: 826783599575 (user/agent)
Goal: prove end-to-end that CloudFront signed cookies can gate streaming media
from a private S3 bucket with Range request support. **Outcome: proven, 5/5
checks passed.**

## 1. Legacy bucket audit — `karaoke-pimpshizzle` (read-only, untouched)

| Prefix | Objects | Size | Contents |
|---|---|---|---|
| `karaoke/in/` | 1 | 12.6 MB | one job's `source.mp4` (job `2d88b7a5-...`) |
| `karaoke/out/` | 9 | 636 MB | same job: 6 stem WAVs (~100 MB each), `karaoke_six_stem.mp4`, `manifest.json` |
| `karaoke/pub/` | 295 | 7.45 GB | **36 published job folders** |

Example keys:

- `karaoke/in/2d88b7a5-e8c6-411d-89df-2b0f9fa084f7/source.mp4`
- `karaoke/out/2d88b7a5-e8c6-411d-89df-2b0f9fa084f7/stems/vocals.wav`
- `karaoke/pub/07589117e9ab/stems.json`
- `karaoke/pub/07589117e9ab/stems/bass.m4a` (~6.6 MB AAC per stem)
- `karaoke/pub/07589117e9ab/stems/stems_merged.webm`
- `karaoke/pub/07589117e9ab/multi-track.mp4`

**Inheritable content: yes.** Each `karaoke/pub/<jobid>/` folder is a complete
published track: `stems.json` manifest (version 3 — title, duration, timeline
with sample rate, video codec metadata, 6 stems with `default_gain` and
`channel_offset`, merged-audio reference), per-stem AAC `.m4a` files, a
`stems_merged.webm`, `video.mp4`, and `multi-track.mp4`. 36 tracks ready to
seed the new app's catalog. `karaoke/out/` uses an older manifest (no
`version` field, WAV stems) — less useful; `pub/` is the inheritance target.

**Legacy infrastructure found (read-only):** a second, unlabeled CloudFront
distribution exists on the account — `E1TTUZICNONOHR`
(`dfpxpuycadacf.cloudfront.net`), Deployed, Enabled, empty comment. Origin:
`karaoke-pimpshizzle.s3.us-east-1.amazonaws.com` via OAC `E2ISRMK2Y8S6Y6`
(no OAI). Default behavior has **no trusted key groups** (TrustedKeyGroups
Enabled=false) — the legacy k25 delivery distro serves that bucket without
signed-cookie gating. Left untouched; excluded from spike teardown.

## 2. Resources created (all prefixed `shizzle-spike-`, tagged `project=shizzle-spike`)

| Resource | ID / ARN |
|---|---|
| S3 bucket | `shizzle-spike-media-9abf4c` (`arn:aws:s3:::shizzle-spike-media-9abf4c`), private, all public access blocked |
| — objects | `media/vocals.m4a` (5,930,926 B, from k25 job `47bae048e13c`), `media/test.txt` (16 B) |
| CloudFront public key | `KRNC9VLVC15DN` (`shizzle-spike-key`) |
| CloudFront key group | `cfad272c-929b-45be-93db-501dd50e5948` (`shizzle-spike-keygroup`) |
| Origin access control | `E2FU4GKEQOF0HR` (`shizzle-spike-oac`, sigv4/always/s3) |
| Distribution | `EYDQD3CPYVRRU` · `d2wr9nfx0lr3a2.cloudfront.net` · `arn:aws:cloudfront::826783599575:distribution/EYDQD3CPYVRRU` |
| RSA 2048 keypair | `X:\GitHub\shizzle\secrets\cloudfront-spike\{private_key,public_key}.pem` (gitignored) |

Distribution config: single S3 origin via OAC (S3OriginConfig OAI empty);
default behavior GET/HEAD only, redirect-to-https, managed CachingOptimized
cache policy (`658327ea-f89d-4fab-a63d-7e88639e58f6`), TrustedKeyGroups =
the spike key group. Bucket policy allows `s3:GetObject` to service principal
`cloudfront.amazonaws.com` conditioned on `AWS:SourceArn` = the distribution
ARN — nothing else can read the bucket.

Note: tags could not be applied to the CloudFront public key, key group, or
OAC — those CloudFront sub-resources do not support tagging. The
`shizzle-spike-` name prefix is the teardown handle for them.

## 3. Proof matrix (`spikes/signed-cookie-proof/prove.py`, run 2026-08-02 ~18:04)

| # | Check | Expected | Got | Result |
|---|---|---|---|---|
| 1 | Unauthenticated GET `/media/vocals.m4a` | 403 | 403 | PASS |
| 2 | GET with signed cookies (custom policy, +10 min expiry) | 200 | 200, `content-type: audio/mp4` | PASS |
| 3a | `Range: bytes=0-1023` with cookies | 206 | 206, `Content-Range: bytes 0-1023/5930926` | PASS |
| 3b | `Range: bytes=2000000-2000999` with cookies | 206 | 206, `Content-Range: bytes 2000000-2000999/5930926` | PASS |
| 4 | GET with expired-policy cookies (expiry 1 h in the past) | 403 | 403 | PASS |

Cookie mechanics proven: custom policy (`Resource:
https://<domain>/media/*`, `DateLessThan` epoch), RSA-SHA1 PKCS#1 v1.5
signature, CloudFront base64 variant (`+/=` → `-~_`), sent as
`CloudFront-Policy` / `CloudFront-Signature` / `CloudFront-Key-Pair-Id`.
Signing is purely local (Python `cryptography`); no AWS calls at request time.

## 4. Gotchas

- **Machine env override.** This PC has a global `AWS_ENDPOINT_URL` pointing
  at Cloudflare R2. Every AWS invocation must `unset AWS_ENDPOINT_URL` (the
  project `.env` also pins it) or S3 calls silently hit R2.
- **OAC and signed cookies are independent layers that compose cleanly.**
  OAC authenticates *CloudFront→S3* (sigv4, bucket policy on
  `AWS:SourceArn`); trusted key groups authenticate *viewer→CloudFront*.
  With TrustedKeyGroups enabled, an uncookied request is rejected at the edge
  (403 `MissingKey`) and never reaches S3. No interplay problems observed.
- **OAC config detail:** the origin still requires `S3OriginConfig` with an
  *empty* `OriginAccessIdentity` string alongside `OriginAccessControlId` —
  omitting `S3OriginConfig` entirely fails validation.
- **Cache policy vs cookies:** managed CachingOptimized (no cookies in cache
  key) is correct — CloudFront consumes the three `CloudFront-*` cookies
  itself; they must NOT be forwarded/keyed, or the cache fragments per user.
- **Range worked with zero extra config.** CloudFront handles `Range`
  natively with the standard cache policy; both head-of-file and mid-file
  ranges returned correct 206/`Content-Range`. No `Accept-Ranges` forwarding
  or origin-request policy needed.
- **CLI shape trap:** `aws cloudfront create-distribution-with-tags` wants
  the `file://` JSON to contain `DistributionConfig` + `Tags` at top level —
  wrapping them in a `DistributionConfigWithTags` object (as the parameter
  name suggests) fails validation.
- **Propagation was fast:** distribution created ~17:57, `Status=Deployed`
  by 17:59:55 (<3 min) — far below the documented 5-20 min; still poll
  rather than assume.
- **Policy JSON must be compact** (no whitespace) before base64/signing —
  padded JSON invalidates the signature match.

## 5. Files

- Proof script: `X:\GitHub\shizzle\spikes\signed-cookie-proof\prove.py`
- Teardown commands: `X:\GitHub\shizzle\spikes\signed-cookie-proof\teardown.md`
- Key material: `X:\GitHub\shizzle\secrets\cloudfront-spike\` (gitignored)

DONE: builder | Spike 0.2 proven 5/5 — signed cookies gate private-S3 CloudFront streaming with Range support; 36 inheritable tracks found in karaoke/pub/
