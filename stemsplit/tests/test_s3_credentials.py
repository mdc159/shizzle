"""Ensure worker clients preserve SDK-managed credentials, including tokens."""

from unittest.mock import patch

import boto3

import s3_ops


def test_worker_uses_environment_session_token(monkeypatch):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "temporary-access")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "temporary-secret")
    monkeypatch.setenv("AWS_SESSION_TOKEN", "temporary-token")
    session = boto3.Session()
    with patch.object(s3_ops.boto3, "client", side_effect=session.client):
        client = s3_ops.create_s3_client()
    credentials = client._request_signer._credentials
    assert credentials.method == "env"
    assert credentials.get_frozen_credentials().token == "temporary-token"


def test_worker_leaves_provider_resolution_to_sdk():
    with patch.object(s3_ops.boto3, "client") as factory:
        s3_ops.create_s3_client()
    assert not any(key.startswith("aws_") for key in factory.call_args.kwargs)


def test_worker_ignores_foreign_endpoint_override(monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL_S3", "https://r2.example.test")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "synthetic")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "synthetic")
    session = boto3.Session()
    with patch.object(s3_ops.boto3, "client", side_effect=session.client):
        client = s3_ops.create_s3_client()
    assert client.meta.endpoint_url.endswith(".amazonaws.com")
