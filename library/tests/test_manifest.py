"""Manifest contract tests (spec §5, Phase 1.3).

The manifest carries stem gains in dB (``default_gain_db``), never linear.
The v2 field ``default_gain`` was written as linear 0 (silence) while the UI
treated the value as unity — renaming to an explicit dB field kills that
latent bug at the contract level.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from shizzle_server.processing import STEM_ID_MAP, STEM_ORDER, write_manifest


def _write(tmp_path: Path, with_m4a: bool = False) -> dict[str, Any]:
    stems_dir = tmp_path / "stems"
    stems_dir.mkdir()
    if with_m4a:
        for stem in STEM_ORDER:
            (stems_dir / f"{stem}.m4a").write_bytes(b"")
    manifest_path = write_manifest(stems_dir, {}, 30.0, "golden")
    assert manifest_path == tmp_path / "stems.json"
    data: dict[str, Any] = json.loads(manifest_path.read_text())
    return data


def test_manifest_version_is_3(tmp_path: Path) -> None:
    assert _write(tmp_path)["version"] == 3


def test_stems_carry_default_gain_db_not_default_gain(tmp_path: Path) -> None:
    data = _write(tmp_path)
    assert len(data["stems"]) == len(STEM_ORDER)
    for entry in data["stems"]:
        assert entry["default_gain_db"] == 0.0
        assert "default_gain" not in entry


def test_stem_ids_and_file_extension_selection(tmp_path: Path) -> None:
    wav = _write(tmp_path)
    assert [s["id"] for s in wav["stems"]] == [STEM_ID_MAP[s] for s in STEM_ORDER]
    assert all(s["file"].endswith(".wav") for s in wav["stems"])

    # separate tmp dir for the m4a case
    sub = tmp_path / "m4a_case"
    sub.mkdir()
    m4a = _write(sub, with_m4a=True)
    assert all(s["file"].endswith(".m4a") for s in m4a["stems"])
