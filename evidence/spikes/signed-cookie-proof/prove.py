"""Spike 0.2 proof: CloudFront signed cookies gate a private S3 bucket (OAC).

Proves four things against the spike distribution:
  1. Unauthenticated GET of the stem -> 403
  2. GET with valid signed cookies (custom policy, short expiry) -> 200
  3. Range requests (head-of-file and mid-file) with cookies -> 206 + Content-Range
  4. Cookies signed over an already-expired policy -> 403

Uses Python stdlib for HTTP and the `cryptography` package for RSA-SHA1
signing (CloudFront's signed-cookie signature scheme). No boto3/botocore
calls are needed at runtime -- signing is purely local.

Usage:
    python prove.py [--domain d2wr9nfx0lr3a2.cloudfront.net]
                    [--key-pair-id KRNC9VLVC15DN]
                    [--private-key ../../secrets/cloudfront-spike/private_key.pem]
                    [--path /media/vocals.m4a]
"""

import argparse
import base64
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

DEFAULT_DOMAIN = "d2wr9nfx0lr3a2.cloudfront.net"
DEFAULT_KEY_PAIR_ID = "KRNC9VLVC15DN"
DEFAULT_KEY_PATH = Path(__file__).resolve().parents[2] / "secrets" / "cloudfront-spike" / "private_key.pem"
DEFAULT_PATH = "/media/vocals.m4a"


def cloudfront_b64(data: bytes) -> str:
    """Base64 with CloudFront's URL-safe substitutions (+ -> -, = -> _, / -> ~)."""
    return (
        base64.b64encode(data)
        .decode("ascii")
        .replace("+", "-")
        .replace("=", "_")
        .replace("/", "~")
    )


def build_policy(resource: str, expires_epoch: int) -> str:
    """Compact custom-policy JSON (whitespace matters: CloudFront rejects padded JSON)."""
    policy = {
        "Statement": [
            {
                "Resource": resource,
                "Condition": {"DateLessThan": {"AWS:EpochTime": expires_epoch}},
            }
        ]
    }
    return json.dumps(policy, separators=(",", ":"))


def make_signed_cookies(private_key_path: Path, key_pair_id: str, resource: str, expires_epoch: int) -> dict:
    """Build the three CloudFront signed-cookie values for a custom policy."""
    key = serialization.load_pem_private_key(private_key_path.read_bytes(), password=None)
    policy = build_policy(resource, expires_epoch)
    signature = key.sign(policy.encode("utf-8"), padding.PKCS1v15(), hashes.SHA1())
    return {
        "CloudFront-Policy": cloudfront_b64(policy.encode("utf-8")),
        "CloudFront-Signature": cloudfront_b64(signature),
        "CloudFront-Key-Pair-Id": key_pair_id,
    }


def http_get(url: str, cookies: dict | None = None, byte_range: str | None = None) -> tuple[int, dict, int]:
    """GET url; return (status, selected headers, body bytes read). Reads at most 64 KiB."""
    req = urllib.request.Request(url)
    if cookies:
        req.add_header("Cookie", "; ".join(f"{k}={v}" for k, v in cookies.items()))
    if byte_range:
        req.add_header("Range", byte_range)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            body = resp.read(65536)
            headers = {k: v for k, v in resp.headers.items()
                       if k.lower() in ("content-range", "content-length", "content-type", "x-cache")}
            return resp.status, headers, len(body)
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers.items()), 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--domain", default=DEFAULT_DOMAIN)
    parser.add_argument("--key-pair-id", default=DEFAULT_KEY_PAIR_ID)
    parser.add_argument("--private-key", type=Path, default=DEFAULT_KEY_PATH)
    parser.add_argument("--path", default=DEFAULT_PATH)
    args = parser.parse_args()

    url = f"https://{args.domain}{args.path}"
    resource = f"https://{args.domain}/media/*"
    now = int(time.time())
    results = []

    def record(name: str, expected: str, status: int, headers: dict, note: str = "") -> bool:
        ok = str(status) == expected.split()[0]
        cr = headers.get("Content-Range", "")
        line = f"[{'PASS' if ok else 'FAIL'}] {name}: expected {expected}, got {status}"
        if cr:
            line += f" (Content-Range: {cr})"
        if note:
            line += f" -- {note}"
        print(line)
        results.append(ok)
        return ok

    # 1. Unauthenticated
    status, headers, _ = http_get(url)
    record("unauthenticated GET", "403", status, headers)

    # 2. Valid signed cookies (10 minute expiry)
    cookies = make_signed_cookies(args.private_key, args.key_pair_id, resource, now + 600)
    status, headers, nread = http_get(url, cookies=cookies)
    record("signed-cookie GET", "200", status, headers,
           note=f"content-type={headers.get('Content-Type')}, read {nread} bytes")

    # 3a. Range head-of-file
    status, headers, _ = http_get(url, cookies=cookies, byte_range="bytes=0-1023")
    ok = record("Range bytes=0-1023", "206", status, headers)
    if ok and not headers.get("Content-Range", "").startswith("bytes 0-1023/"):
        print("       WARNING: Content-Range does not match requested range")
        results[-1] = False

    # 3b. Range mid-file
    status, headers, _ = http_get(url, cookies=cookies, byte_range="bytes=2000000-2000999")
    ok = record("Range bytes=2000000-2000999", "206", status, headers)
    if ok and not headers.get("Content-Range", "").startswith("bytes 2000000-2000999/"):
        print("       WARNING: Content-Range does not match requested range")
        results[-1] = False

    # 4. Expired policy (expired 1 hour ago)
    expired = make_signed_cookies(args.private_key, args.key_pair_id, resource, now - 3600)
    status, headers, _ = http_get(url, cookies=expired)
    record("expired-policy GET", "403", status, headers)

    print(f"\n{sum(results)}/{len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
