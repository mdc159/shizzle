"""AWS providers and explicit S3-compatible credentials stay separate."""

from unittest.mock import patch

import pytest

from shizzle_server.lib.s3_multipart import S3MultipartUploader, S3UploadConfig


def test_default_aws_preserves_provider_chain():
    with patch("shizzle_server.lib.s3_multipart.boto3.session.Session") as session:
        S3MultipartUploader(S3UploadConfig(endpoint=None, bucket="media"))
    session.assert_called_once_with(region_name="us-east-1")


def test_explicit_temporary_credentials_include_session_token():
    with patch("shizzle_server.lib.s3_multipart.boto3.session.Session") as session:
        S3MultipartUploader(
            S3UploadConfig(None, "media", "access", "secret", session_token="token")
        )
    session.assert_called_once_with(
        region_name="us-east-1",
        aws_access_key_id="access",
        aws_secret_access_key="secret",
        aws_session_token="token",
    )


@pytest.mark.parametrize("endpoint", ["https://r2.example.test", "https://s3api-us-ks-2.runpod.io"])
def test_custom_storage_cannot_silently_use_ambient_aws(endpoint):
    with (
        patch("shizzle_server.lib.s3_multipart.boto3.session.Session") as session,
        pytest.raises(ValueError, match="explicit credentials"),
    ):
        S3MultipartUploader(S3UploadConfig(endpoint, "media"))
    session.assert_not_called()


@pytest.mark.parametrize(
    "access,secret,token", [("access", None, None), (None, "secret", None), (None, None, "token")]
)
def test_incomplete_explicit_credentials_fail_before_network(access, secret, token):
    with pytest.raises(ValueError):
        S3MultipartUploader(S3UploadConfig(None, "media", access, secret, session_token=token))
