# CloudFront media delivery

CloudFront is the production media edge for Shizzle. The application itself is
served by Caddy at `shizzle.systems`; CloudFront fronts private S3 media.

## Current production path

```text
authenticated browser
    ↓ requests manifest grant
FastAPI
    ↓ returns expiring file-scoped URLs
CloudFront distribution ELKN8VGSX0M64
    ↓ OAC
private S3 bucket karaoke-pimpshizzle
```

- Six AAC stems stream directly with CORS and HTTP byte ranges.
- The bounded audio-less video is fetched from the same edge and staged as one
  revocable browser Blob.
- Signed query values are never written to telemetry or durable browser
  evidence.
- Same-origin `/cdn` remains the tested fallback path.

The active library size is runtime data; query the authenticated application
instead of treating a past audit's track count as current inventory.

## Files in this directory

`apex-change.json` is environment-specific DNS change data for the VPS A
record and the `www` CNAME. Compare its target with the intended VPS before
using it; it is not automatically applied. Current CloudFront configuration
also appears in `.env.example` and `../vps/Caddyfile.prod`.

The retired spike-bucket/CloudFront application-apex provisioning scripts are
not part of the current tree. Infrastructure must satisfy the requirements
below; this directory is not an automated installer.

## Current infrastructure requirements

Provision a private S3 media bucket and an OAC-backed CloudFront distribution
whose media paths map to `tracks/`. Attach the trusted key group matching the
API's `CLOUDFRONT_KEY_PAIR_ID` and mounted signing private key. The bucket policy
must allow the distribution ARN. Configure Range GET/HEAD delivery and CORS
for the application origin; preserve the required signed-URL gate. The API's
`CLOUDFRONT_DOMAIN` and the same-origin fallback upstream in `Caddyfile.prod`
must identify the intended distribution. Application DNS and TLS belong to
the VPS/Caddy path. This repository currently has no replacement automated
provisioner for that complete topology.

## Troubleshooting

For a 403, missing object, or failed seek:

1. Confirm authentication and request a fresh manifest.
2. Test the exact signed object URL.
3. Require HTTP 206 for a small byte-range request.
4. Check CORS, content type, object identity, URL expiry, and OAC access.
5. Use `../../docs/playback-troubleshooting.md` before changing the delivery
   design.

CloudFront origin names must be DNS names. OAC bucket access is scoped to the
distribution ARN, so any replacement distribution requires a matching bucket
policy update.
