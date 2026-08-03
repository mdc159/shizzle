"""Cloud-media resolution: read a track's manifest from S3 and rewrite its
media refs to same-origin CDN paths (design spec §3, implementation Phase 4).

The player never talks to S3 directly. It fetches the manifest here (behind the
passcode gate), and the manifest's ``video`` / ``stems[].file`` come back as
same-origin paths under ``media_cookie_path`` (/cdn). Caddy reverse-proxies
those to CloudFront, which enforces the signed-cookie gate over the private
bucket. Range requests work natively through CloudFront (spike 0.2).
"""

from __future__ import annotations

import json
import os
from functools import lru_cache
from typing import Any

import boto3

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
    """Same-origin CDN path for one media file, e.g. /cdn/tracks/<id>/1/stems/vocals.m4a."""
    rel = file.lstrip("/")
    return f"{settings.media_cookie_path}/{s3_prefix}/{rel}"


def rewrite_cloud_manifest(
    settings: Settings, manifest: dict[str, Any], s3_prefix: str
) -> dict[str, Any]:
    """Return a copy of the manifest with video + stem files as /cdn paths."""
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
