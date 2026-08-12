from __future__ import annotations

from pathlib import Path

import pytest

from shizzle_server.publish.media_audit import (
    MediaAuditError,
    inspect_mp4_atoms,
    max_keyframe_interval,
)


def _atom(kind: bytes, body: bytes) -> bytes:
    return (len(body) + 8).to_bytes(4, "big") + kind + body


def test_mp4_atom_order_detects_fast_start(tmp_path: Path):
    path = tmp_path / "fast.mp4"
    path.write_bytes(_atom(b"ftyp", b"a" * 8) + _atom(b"moov", b"b" * 8) + _atom(b"mdat", b"c" * 8))
    layout = inspect_mp4_atoms(path)
    assert layout.fast_start
    assert layout.atoms == ("ftyp", "moov", "mdat")


def test_mp4_atom_order_rejects_moov_at_end(tmp_path: Path):
    path = tmp_path / "slow.mp4"
    path.write_bytes(_atom(b"ftyp", b"a" * 8) + _atom(b"mdat", b"c" * 8) + _atom(b"moov", b"b" * 8))
    assert not inspect_mp4_atoms(path).fast_start


def test_invalid_atom_size_fails_closed(tmp_path: Path):
    path = tmp_path / "broken.mp4"
    path.write_bytes((999).to_bytes(4, "big") + b"mdat" + b"short")
    with pytest.raises(MediaAuditError, match="invalid MP4 atom"):
        inspect_mp4_atoms(path)


def test_max_keyframe_interval_includes_start_and_tail():
    assert max_keyframe_interval([0.0, 2.0, 4.0], 5.5) == pytest.approx(2.0)
    assert max_keyframe_interval([], 5.5) is None
