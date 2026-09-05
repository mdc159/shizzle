# Domain and CDN verification

Use [current AWS requirements](../../deploy/aws/README.md) and
[deployment operations](../../docs/AUTOMATION.md) for the supported topology:
Caddy serves the application; CloudFront serves private S3 media. This
retained test entry point replaces the retired provisioning timeline.

## Checks to preserve

1. Verify application DNS resolves to the intended VPS, then check HTTPS and
   application health separately from media delivery.
2. Authenticate normally and request a fresh manifest. Test a signed media
   object before the grant expires; require successful full and byte-range
   retrieval, correct content type, and browser CORS behavior.
3. Confirm the CloudFront distribution is authorized by the S3 bucket policy
   and uses the intended private origin and trusted signing key group.
4. Verify unsigned private media requests are rejected. A rejection alone does
   not prove the object exists or that an authenticated request will succeed.
5. Test browser playback after grant creation, including large backward and
   forward seeks. Record sanitized paths, response status, and playback health;
   never retain signed query strings, cookies, or credentials.

A registrar operation, DNS resolution, certificate issuance, application health,
and playable media are separate checks. Distinguish them when reporting a
failure. Infrastructure IDs in old experimental outputs are not live-state
proof. See [Range/auth experiment](RESULTS-0.2.md) and
[playback test procedures](../../docs/TESTING.md).
