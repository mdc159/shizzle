"""
Callback handling for Runpod Demucs handler.

Handles progress updates and completion callbacks with authentication.
Supports HMAC-SHA256 signatures with bearer token fallback.
"""

import hashlib
import hmac
import json
import os
import time
from datetime import datetime
from typing import Any

import requests


def sign_payload(payload: dict[str, Any], secret: str) -> tuple[str, str]:
    """
    Generate HMAC-SHA256 signature for payload.

    Signature format: HMAC-SHA256(secret, "{timestamp}.{json_payload}")

    Args:
        payload: Dict to sign
        secret: Shared secret for HMAC

    Returns:
        Tuple of (signature_hex, timestamp_str)
    """
    timestamp = str(int(time.time()))
    message = f"{timestamp}.{json.dumps(payload, sort_keys=True)}"
    signature = hmac.new(
        secret.encode(),
        message.encode(),
        hashlib.sha256
    ).hexdigest()
    return signature, timestamp


def _get_auth_headers() -> dict[str, str]:
    """
    Get authentication headers for callbacks.

    Priority:
    1. HMAC signature (if CALLBACK_SECRET set)
    2. Bearer token (if CALLBACK_TOKEN set)
    3. No auth (fallback)

    Returns:
        Headers dict (may be empty if no auth configured)
    """
    headers: dict[str, str] = {}

    # Note: actual signing happens in send_callback for HMAC
    # This just checks if token fallback should be used
    secret = os.getenv("CALLBACK_SECRET")
    if secret:
        # HMAC auth - headers added per-request with payload
        return headers

    token = os.getenv("CALLBACK_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    return headers


def send_callback(payload: dict[str, Any]) -> None:
    """
    Send authenticated callback to VPS webhook.

    Uses HMAC signature if CALLBACK_SECRET is set,
    falls back to bearer token if CALLBACK_TOKEN is set,
    or sends unauthenticated if neither is configured.

    Args:
        payload: Callback payload to send
    """
    callback_url = os.getenv("CALLBACK_URL")
    if not callback_url:
        print("No CALLBACK_URL set, skipping callback", flush=True)
        return

    headers = {"Content-Type": "application/json"}

    # Try HMAC signature first
    secret = os.getenv("CALLBACK_SECRET")
    if secret:
        signature, timestamp = sign_payload(payload, secret)
        headers["X-Signature"] = signature
        headers["X-Timestamp"] = timestamp
        print(f"Sending signed callback to {callback_url}", flush=True)
    else:
        # Fallback to bearer token
        token = os.getenv("CALLBACK_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
            print(f"Sending token-authenticated callback to {callback_url}", flush=True)
        else:
            print(f"Sending unauthenticated callback to {callback_url}", flush=True)

    try:
        resp = requests.post(callback_url, json=payload, headers=headers, timeout=30)
        print(f"Callback response: {resp.status_code}", flush=True)
    except Exception as e:
        print(f"Callback failed: {e}", flush=True)


def send_progress(job_id: str, stage: str, progress: int) -> None:
    """
    Send progress update to n8n webhook (non-blocking, fire-and-forget).

    Derives progress URL from CALLBACK_URL by replacing endpoint path.
    e.g., https://n8n.example.com/karaoke-publish -> /karaoke-progress

    Stages: extracting, splitting, encoding, complete

    Args:
        job_id: Job ID for progress update
        stage: Processing stage name
        progress: Progress percentage (0-100)
    """
    callback_url = os.getenv("CALLBACK_URL")
    if not callback_url:
        return  # No callback configured, skip silently

    # Derive progress URL from callback URL
    progress_url = callback_url.rsplit("/", 1)[0] + "/karaoke-progress"

    payload = {
        "job_id": job_id,
        "stage": stage,
        "progress": progress,
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }

    headers = {"Content-Type": "application/json"}

    # Add auth headers if configured
    secret = os.getenv("CALLBACK_SECRET")
    if secret:
        signature, timestamp = sign_payload(payload, secret)
        headers["X-Signature"] = signature
        headers["X-Timestamp"] = timestamp

    token = os.getenv("CALLBACK_TOKEN")
    if not secret and token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        print(f"Progress: {stage} ({progress}%) -> {progress_url}", flush=True)
        requests.post(
            progress_url,
            json=payload,
            headers=headers,
            timeout=5,  # Short timeout - don't block processing
        )
    except Exception as e:
        print(f"Progress callback failed (non-fatal): {e}", flush=True)


def send_completion(result: dict[str, Any]) -> None:
    """
    Send job completion callback.

    Args:
        result: Job result payload with status, uploads, etc.
    """
    send_callback(result)


def send_error(job_id: str, error: str) -> None:
    """
    Send error callback for failed job.

    Args:
        job_id: Job ID that failed
        error: Error message
    """
    send_callback({
        "status": "error",
        "job_id": job_id,
        "error": error,
    })
