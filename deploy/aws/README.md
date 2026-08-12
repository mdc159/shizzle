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

This media-delivery portion is complete for the accepted 27-track library.

## Scripts in this directory

The numbered scripts record the original distribution and DNS setup. They are
useful for troubleshooting or rebuilding infrastructure, not a current phased
implementation plan.

All scripts load credentials from the gitignored repository `.env`, clear an
ambient `AWS_ENDPOINT_URL`, and avoid printing secrets.

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
