"""Guards for invariant F1 (single linear migration chain, numeric prefix ==
revision id, explicit down_revision, paired downgrade) — runnable without
Postgres. The real upgrade/downgrade is exercised by the contract suite and
the postgres-contract CI job (alembic downgrade -1 && upgrade head)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory

VERSIONS = Path(__file__).resolve().parents[1] / "alembic" / "versions"


def _script() -> ScriptDirectory:
    config = Config()
    config.set_main_option("script_location", str(VERSIONS.parent))
    return ScriptDirectory.from_config(config)


def _load(revision_id: str):
    path = VERSIONS / f"{revision_id}.py"
    spec = importlib.util.spec_from_file_location(revision_id, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_single_linear_chain_with_matching_ids():
    script = _script()
    heads = script.get_heads()
    assert len(heads) == 1, f"migration chain has branched: {heads}"
    assert heads[0] == "0005_job_artist"

    order = list(script.walk_revisions())
    assert [r.revision for r in order] == [
        "0005_job_artist",
        "0004_worker_heartbeats",
        "0003_playback_event_bigint",
        "0002_playback_telemetry",
        "0001_initial",
    ]
    assert order[-1].down_revision is None  # base
    for revision in order[:-1]:
        assert revision.down_revision is not None  # explicit, never implied


def test_every_revision_id_matches_its_filename():
    on_disk = {path.stem for path in VERSIONS.glob("*.py") if path.stem != "__init__"}
    script = _script()
    assert {r.revision for r in script.walk_revisions()} == on_disk


def test_0005_adds_job_artist_and_rolls_back():
    module = _load("0005_job_artist")
    assert module.revision == "0005_job_artist"
    assert module.down_revision == "0004_worker_heartbeats"
    # Paired real downgrade (F1): both bodies reference the artist column.
    import inspect

    assert "artist" in inspect.getsource(module.upgrade)
    assert "server_default" in inspect.getsource(module.upgrade)
    assert "drop_column" in inspect.getsource(module.downgrade)
