"""Tests for input validation and the staging path/knob semantics."""

import pytest

from config import (
    DEFAULT_AAC_BITRATE,
    InputValidationError,
    stem_aac_bitrate,
    validate_input,
    validate_s3_paths,
)


def base_input() -> dict:
    return {
        "job_id": "job-123",
        "bucket": "shizzle-media",
        "input_key": "tracks/trk_abc/gen-001/staging/source.mp4",
        "output_prefix": "tracks/trk_abc/gen-001/staging/",
        "metadata": {"title": "Song", "artist": "Band"},
    }


class TestPaths:
    def test_valid_staging_paths_parse_track_and_generation(self):
        track_id, generation = validate_s3_paths(
            "tracks/trk_abc/gen-001/staging/source.mp4",
            "tracks/trk_abc/gen-001/staging/",
        )
        assert track_id == "trk_abc"
        assert generation == "gen-001"

    def test_traversal_rejected(self):
        with pytest.raises(InputValidationError, match="traversal"):
            validate_s3_paths(
                "tracks/a/../b/staging/source.mp4", "tracks/a/b/staging/"
            )

    def test_legacy_karaoke_layout_rejected(self):
        with pytest.raises(InputValidationError):
            validate_s3_paths(
                "karaoke/in/rec123/source.mp4", "karaoke/out/rec123/"
            )

    def test_non_staging_output_rejected(self):
        with pytest.raises(InputValidationError):
            validate_s3_paths(
                "tracks/a/b/staging/source.mp4", "tracks/a/b/"
            )


class TestValidateInput:
    def test_valid_input(self):
        cfg = validate_input(base_input())
        assert cfg.track_id == "trk_abc"
        assert cfg.generation == "gen-001"
        assert cfg.model == "htdemucs_6s"
        assert cfg.create_multitrack_mp4 is True
        assert cfg.aac_bitrate == DEFAULT_AAC_BITRATE
        assert cfg.metadata.title == "Song"

    def test_missing_job_id(self):
        inp = base_input()
        del inp["job_id"]
        with pytest.raises(InputValidationError, match="job_id"):
            validate_input(inp)

    def test_missing_bucket_without_env(self, monkeypatch):
        monkeypatch.delenv("AWS_S3_BUCKET", raising=False)
        inp = base_input()
        del inp["bucket"]
        with pytest.raises(InputValidationError, match="bucket"):
            validate_input(inp)

    def test_multitrack_flag_env_default(self, monkeypatch):
        monkeypatch.setenv("CREATE_MULTITRACK_MP4", "false")
        cfg = validate_input(base_input())
        assert cfg.create_multitrack_mp4 is False

    def test_multitrack_input_overrides_env(self, monkeypatch):
        monkeypatch.setenv("CREATE_MULTITRACK_MP4", "false")
        inp = base_input()
        inp["create_multitrack_mp4"] = True
        cfg = validate_input(inp)
        assert cfg.create_multitrack_mp4 is True

    def test_bad_bitrate_rejected(self):
        inp = base_input()
        inp["aac_bitrate"] = "320000"
        with pytest.raises(InputValidationError, match="aac_bitrate"):
            validate_input(inp)


class TestBitrateKnob:
    def test_default(self, monkeypatch):
        monkeypatch.delenv("STEM_AAC_BITRATE", raising=False)
        assert stem_aac_bitrate() == "256k"

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("STEM_AAC_BITRATE", "256k")
        assert stem_aac_bitrate() == "256k"

    def test_invalid_env_rejected(self, monkeypatch):
        monkeypatch.setenv("STEM_AAC_BITRATE", "lossless")
        with pytest.raises(InputValidationError):
            stem_aac_bitrate()
