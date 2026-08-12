"""Resolve private-S3 manifests into authenticated browser-delivery URLs.

Production manifests use file-scoped, expiring CloudFront signed URLs so seven
concurrent Range streams go directly to the edge instead of relaying through
the VPS. CloudFront still fronts private S3 through OAC. Environments without
CloudFront retain the same-origin ``/cdn`` fallback.
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

import boto3

from . import cloudfront
from .settings import Settings


@lru_cache(maxsize=1)
def _s3_client(region: str, endpoint: str) -> Any:
    # Machine gotcha: a global AWS_ENDPOINT_URL points this PC at Cloudflare R2.
    # Pop it so real-AWS S3 calls are not silently rerouted (spike 0.2 gotcha).
    os.environ.pop("AWS_ENDPOINT_URL", None)
    os.environ.pop("AWS_ENDPOINT_URL_S3", None)
    kwargs: dict[str, Any] = {"region_name": region}
    if endpoint:
        kwargs["endpoint_url"] = endpoint
    return boto3.client("s3", **kwargs)


def s3_client(settings: Settings) -> Any:
    return _s3_client(settings.aws_region, settings.aws_endpoint_url)


def load_s3_manifest(settings: Settings, manifest_key: str) -> dict[str, Any]:
    client = s3_client(settings)
    body = client.get_object(Bucket=settings.s3_media_bucket, Key=manifest_key)["Body"]
    data: dict[str, Any] = json.loads(body.read())
    return data


def _media_url(settings: Settings, s3_prefix: str, file: str) -> str:
    """Direct signed edge URL in production; same-origin fallback otherwise."""
    rel = file.lstrip("/")
    key = f"{s3_prefix}/{rel}"
    if settings.cloudfront_enabled:
        return cloudfront.signed_url(settings, key)
    return f"{settings.media_cookie_path}/{key}"


def rewrite_cloud_manifest(
    settings: Settings, manifest: dict[str, Any], s3_prefix: str
) -> dict[str, Any]:
    """Return a copy with media files rewritten for authenticated delivery."""
    out = dict(manifest)
    if out.get("video"):
        out["video"] = _media_url(settings, s3_prefix, str(out["video"]))
    stems = []
    for stem in out.get("stems", []):
        s = dict(stem)
        if s.get("file"):
            s["file"] = _media_url(settings, s3_prefix, str(s["file"]))
        stems.append(s)
    out["stems"] = stems
    return out
