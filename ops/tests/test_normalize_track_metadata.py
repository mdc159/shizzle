"""Tests for ops/normalize_track_metadata.py against a temp-file SQLite DB."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
import uuid
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from shizzle_server.db import create_engine, create_session_factory
from shizzle_server.db.models import Base, Track

MODULE_PATH = Path(__file__).resolve().parents[1] / "normalize_track_metadata.py"
SPEC = importlib.util.spec_from_file_location("normalize_track_metadata", MODULE_PATH)
assert SPEC and SPEC.loader
normalizer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = normalizer
SPEC.loader.exec_module(normalizer)

TRACK_FULL = uuid.UUID("11111111-1111-4111-8111-111111111111")
TRACK_MOJIBAKE = uuid.UUID("22222222-2222-4222-8222-222222222222")
TRACK_TOOL = uuid.UUID("33333333-3333-4333-8333-333333333333")
TRACK_DELETED = uuid.UUID("ddeeddee-ddee-4dee-8dee-ddeeddeeddee")

MOJIBAKE_TITLE = "Black (AcÃºstico MTV)"
SEED_CREATED_AT = datetime(2020, 1, 2, 3, 4, 5, tzinfo=UTC)


def base_mapping() -> dict:
    return {
        "schema": "shizzle-track-metadata-fix-v1",
        "tracks": [
            {
                "id": str(TRACK_FULL),
                "expect": {
                    "artist": "",
                    "title": "Van Halen - Runnin' With The Devil (Official Music Video)",
                },
                "artist": "Van Halen",
                "title": "Runnin' With the Devil",
            },
            {
                "id": TRACK_MOJIBAKE.hex[:8],
                "expect_id_prefix": TRACK_MOJIBAKE.hex[:8],
                "artist": "Pearl Jam",
                "title": "Black (Unplugged)",
                "note": "source title has mojibake; match on id prefix",
            },
            {
                "id": TRACK_TOOL.hex[:8],
                "expect_id_prefix": TRACK_TOOL.hex[:8],
                "artist": "Tool",
                "title": "The Pot",
            },
        ],
    }


def seed_track(track_id: uuid.UUID, artist: str, title: str, *, deleted: bool = False) -> Track:
    return Track(
        id=track_id,
        artist=artist,
        title=title,
        duration_seconds=123.0,
        s3_prefix=f"tracks/{track_id}/1",
        generation=1,
        manifest_key=f"tracks/{track_id}/1/manifest.json",
        created_at=SEED_CREATED_AT,
        deleted_at=datetime(2026, 1, 1, tzinfo=UTC) if deleted else None,
    )


class NormalizeTrackMetadataTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.db_path = self.tmp / "test.db"
        self.database_url = f"sqlite+aiosqlite:///{self.db_path}"
        self.mapping_path = self.tmp / "mapping.json"
        self.report_path = self.tmp / "run.json"

    def write_mapping(self, mapping: dict) -> None:
        self.mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

    def seed(self, extra_tracks: list[Track] | None = None) -> None:
        async def _seed() -> None:
            engine = create_engine(self.database_url)
            try:
                async with engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                async with create_session_factory(engine)() as session, session.begin():
                    session.add_all(
                        [
                            seed_track(
                                TRACK_FULL,
                                "",
                                "Van Halen - Runnin' With The Devil (Official Music Video)",
                            ),
                            seed_track(TRACK_MOJIBAKE, "Pearl Jam", MOJIBAKE_TITLE),
                            seed_track(TRACK_TOOL, "TOOL", "The Pot"),
                            seed_track(
                                TRACK_DELETED, "Deleted Artist", "Deleted Title", deleted=True
                            ),
                            *(extra_tracks or []),
                        ]
                    )
            finally:
                await engine.dispose()

        asyncio.run(_seed())

    def read_tracks(self, *, include_deleted: bool = True) -> dict[uuid.UUID, Track]:
        async def _read() -> dict[uuid.UUID, Track]:
            engine = create_engine(self.database_url)
            try:
                async with create_session_factory(engine)() as session:
                    from sqlalchemy import select

                    result = await session.execute(select(Track))
                    return {track.id: track for track in result.scalars().all()}
            finally:
                await engine.dispose()

        return asyncio.run(_read())

    def run_cli(self, *extra: str) -> int:
        argv = [
            "--mapping",
            str(self.mapping_path),
            "--database-url",
            self.database_url,
            *extra,
        ]
        return normalizer.main(argv)

    def test_dry_run_changes_nothing(self) -> None:
        self.seed()
        self.write_mapping(base_mapping())
        before = self.read_tracks()
        code = self.run_cli()
        self.assertEqual(code, 0)
        after = self.read_tracks()
        for track_id, track in before.items():
            current = after[track_id]
            self.assertEqual(current.artist, track.artist)
            self.assertEqual(current.title, track.title)
        self.assertFalse(self.report_path.exists())
        # Note: DEFAULT_REPORT is the committed production run record, so it
        # legitimately exists in a repo checkout; only the explicit --report
        # path proves the dry run wrote nothing.

    def test_malformed_mapping_top_level_is_mapping_error(self) -> None:
        self.seed()
        self.mapping_path.write_text(json.dumps(["not", "an", "object"]), encoding="utf-8")
        self.assertEqual(self.run_cli("--apply", "--report", str(self.report_path)), 2)
        self.assertFalse(self.report_path.exists())

    def test_non_object_entry_collects_violation(self) -> None:
        self.seed()
        mapping = base_mapping()
        mapping["tracks"].append("bogus")
        mapping["tracks"].append(None)
        self.write_mapping(mapping)
        self.assertEqual(self.run_cli("--apply", "--report", str(self.report_path)), 2)
        self.assertFalse(self.report_path.exists())
        self.assertEqual(self.read_tracks()[TRACK_TOOL].artist, "TOOL")

    def test_non_object_expect_collects_violation(self) -> None:
        self.seed()
        mapping = base_mapping()
        mapping["tracks"][0]["expect"] = []
        self.write_mapping(mapping)
        self.assertEqual(self.run_cli("--apply", "--report", str(self.report_path)), 2)
        self.assertFalse(self.report_path.exists())

    def test_full_id_without_expect_aborts(self) -> None:
        self.seed()
        mapping = base_mapping()
        del mapping["tracks"][0]["expect"]
        self.write_mapping(mapping)
        self.assertEqual(self.run_cli("--apply", "--report", str(self.report_path)), 2)
        self.assertFalse(self.report_path.exists())
        self.assertEqual(self.read_tracks()[TRACK_FULL].artist, "")

    def test_full_id_with_prefix_but_without_expect_aborts(self) -> None:
        # A prefix pins identity, not values; it is not a baseline check.
        self.seed()
        mapping = base_mapping()
        mapping["tracks"][0] = {
            "id": str(TRACK_FULL),
            "expect_id_prefix": TRACK_FULL.hex[:8],
            "artist": "Van Halen",
            "title": "Runnin' With the Devil",
        }
        self.write_mapping(mapping)
        self.assertEqual(self.run_cli("--apply", "--report", str(self.report_path)), 2)
        self.assertFalse(self.report_path.exists())
        self.assertEqual(self.read_tracks()[TRACK_FULL].artist, "")

    def test_full_id_delete_with_note_anchor_applies(self) -> None:
        # A delete may anchor on a human-checkable note instead of expect.
        self.seed()
        mapping = base_mapping()
        mapping["tracks"][2] = {
            "id": str(TRACK_TOOL),
            "action": "delete",
            "note": "duplicate of another pressing",
        }
        self.write_mapping(mapping)
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 0)
        self.assertIsNotNone(self.read_tracks()[TRACK_TOOL].deleted_at)
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["counts"]["deleted"], 1)

    def test_apply_aborts_and_rolls_back_when_row_changed_since_validation(self) -> None:
        self.seed()
        planned = [
            normalizer.PlannedChange(
                track_id=TRACK_FULL,
                old_artist="",
                old_title="Van Halen - Runnin' With The Devil (Official Music Video)",
                new_artist="Van Halen",
                new_title="Runnin' With the Devil",
                note="",
            ),
            normalizer.PlannedChange(
                track_id=TRACK_MOJIBAKE,
                old_artist="Pearl Jam",
                old_title=MOJIBAKE_TITLE,
                new_artist="Pearl Jam",
                new_title="Black (Unplugged)",
                note="",
            ),
            # Stale snapshot: the validated values no longer match the row.
            normalizer.PlannedChange(
                track_id=TRACK_TOOL,
                old_artist="Somebody Else",
                old_title="The Pot",
                new_artist="Tool",
                new_title="The Pot",
                note="",
            ),
        ]
        with self.assertRaises(RuntimeError):
            asyncio.run(normalizer.apply_changes(self.database_url, planned))
        after = self.read_tracks()
        self.assertEqual(after[TRACK_FULL].artist, "")
        self.assertEqual(after[TRACK_MOJIBAKE].title, MOJIBAKE_TITLE)
        self.assertEqual(after[TRACK_TOOL].artist, "TOOL")

    def test_apply_aborts_when_library_grew_since_validation(self) -> None:
        self.seed()
        planned = [
            normalizer.PlannedChange(
                track_id=TRACK_FULL,
                old_artist="",
                old_title="Van Halen - Runnin' With The Devil (Official Music Video)",
                new_artist="Van Halen",
                new_title="Runnin' With the Devil",
                note="",
            )
        ]
        with self.assertRaises(RuntimeError):
            asyncio.run(normalizer.apply_changes(self.database_url, planned))
        self.assertEqual(self.read_tracks()[TRACK_FULL].artist, "")

    def test_apply_refuses_to_overwrite_default_report(self) -> None:
        self.seed()
        self.write_mapping(base_mapping())
        existing = self.tmp / "existing-run.json"
        existing.write_text("{}", encoding="utf-8")
        original = normalizer.DEFAULT_REPORT
        normalizer.DEFAULT_REPORT = existing
        try:
            code = self.run_cli("--apply")
        finally:
            normalizer.DEFAULT_REPORT = original
        self.assertEqual(code, 2)
        self.assertEqual(existing.read_text(encoding="utf-8"), "{}")
        self.assertEqual(self.read_tracks()[TRACK_TOOL].artist, "TOOL")

    def test_report_path_that_is_a_directory_aborts_before_applying(self) -> None:
        self.seed()
        self.write_mapping(base_mapping())
        self.report_path.mkdir()
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 2)
        self.assertEqual(self.read_tracks()[TRACK_TOOL].artist, "TOOL")

    def test_report_write_failure_after_commit_prints_record(self) -> None:
        self.seed()
        self.write_mapping(base_mapping())
        stdout = io.StringIO()
        with (
            mock.patch.object(normalizer.os, "replace", side_effect=OSError("boom")),
            contextlib.redirect_stdout(stdout),
        ):
            code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 1)
        self.assertFalse(self.report_path.exists())
        # The changes committed; the full record is recoverable from stdout.
        self.assertEqual(self.read_tracks()[TRACK_TOOL].artist, "Tool")
        marker = '"schema": "shizzle-track-metadata-fix-v1-run"'
        self.assertIn(marker, stdout.getvalue())
        record = json.loads(stdout.getvalue()[stdout.getvalue().index("{"):])
        self.assertEqual(record["counts"]["tracks_updated"], 3)
        self.assertIn("mapping_sha256", record)

    def test_report_uses_mapping_bytes_captured_before_apply(self) -> None:
        # Replacing the mapping after the transaction commits must not change
        # the recorded digest: the record identifies the applied mapping.
        self.seed()
        self.write_mapping(base_mapping())
        expected_sha = hashlib.sha256(self.mapping_path.read_bytes()).hexdigest()
        real_apply = normalizer.apply_changes

        async def replace_mapping_then_apply(database_url, planned):
            self.mapping_path.write_text(
                json.dumps({"schema": "shizzle-track-metadata-fix-v1", "tracks": []}),
                encoding="utf-8",
            )
            return await real_apply(database_url, planned)

        with mock.patch.object(normalizer, "apply_changes", replace_mapping_then_apply):
            code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 0)
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["mapping_sha256"], expected_sha)
        self.assertEqual(report["counts"]["tracks_updated"], 3)

    def test_report_survives_mapping_deleted_during_apply(self) -> None:
        # Deleting the mapping after the commit must not lose the record.
        self.seed()
        self.write_mapping(base_mapping())
        expected_sha = hashlib.sha256(self.mapping_path.read_bytes()).hexdigest()
        real_apply = normalizer.apply_changes

        async def delete_mapping_then_apply(database_url, planned):
            self.mapping_path.unlink()
            return await real_apply(database_url, planned)

        with mock.patch.object(normalizer, "apply_changes", delete_mapping_then_apply):
            code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 0)
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["mapping_sha256"], expected_sha)

    def test_preflight_probe_never_truncates_existing_file(self) -> None:
        self.seed()
        self.write_mapping(base_mapping())
        sentinel = self.tmp / f".{self.report_path.name}.probe"
        sentinel.write_text("do not touch", encoding="utf-8")
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 0)
        self.assertEqual(sentinel.read_text(encoding="utf-8"), "do not touch")

    def test_lock_tracks_table_only_on_postgresql(self) -> None:
        async def _run(dialect: str) -> mock.AsyncMock:
            session = mock.AsyncMock()
            await normalizer._lock_tracks_table(session, dialect)
            return session

        pg_session = asyncio.run(_run("postgresql"))
        pg_session.execute.assert_awaited_once()
        statement = str(pg_session.execute.await_args.args[0])
        self.assertIn("LOCK TABLE tracks IN SHARE ROW EXCLUSIVE MODE", statement)

        sqlite_session = asyncio.run(_run("sqlite"))
        sqlite_session.execute.assert_not_called()

    def test_apply_rewrites_only_artist_and_title(self) -> None:
        self.seed()
        self.write_mapping(base_mapping())
        before = self.read_tracks()
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 0)
        after = self.read_tracks()

        self.assertEqual(after[TRACK_FULL].artist, "Van Halen")
        self.assertEqual(after[TRACK_FULL].title, "Runnin' With the Devil")
        self.assertEqual(after[TRACK_MOJIBAKE].artist, "Pearl Jam")
        self.assertEqual(after[TRACK_MOJIBAKE].title, "Black (Unplugged)")
        self.assertEqual(after[TRACK_TOOL].artist, "Tool")

        for track_id, track in before.items():
            current = after[track_id]
            self.assertEqual(current.created_at, track.created_at)
            self.assertEqual(current.s3_prefix, track.s3_prefix)
            self.assertEqual(current.generation, track.generation)
            self.assertEqual(current.manifest_key, track.manifest_key)
            self.assertEqual(current.duration_seconds, track.duration_seconds)
            self.assertEqual(current.deleted_at, track.deleted_at)

        # The soft-deleted track is never touched.
        self.assertEqual(after[TRACK_DELETED].artist, "Deleted Artist")
        self.assertEqual(after[TRACK_DELETED].title, "Deleted Title")

    def test_expect_mismatch_aborts_with_exit_2_and_changes_nothing(self) -> None:
        self.seed()
        mapping = base_mapping()
        mapping["tracks"][0]["expect"]["title"] = "Something Else Entirely"
        self.write_mapping(mapping)
        before = self.read_tracks()
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 2)
        after = self.read_tracks()
        for track_id, track in before.items():
            self.assertEqual(after[track_id].artist, track.artist)
            self.assertEqual(after[track_id].title, track.title)
        self.assertFalse(self.report_path.exists())

    def test_uncovered_track_aborts(self) -> None:
        extra = seed_track(
            uuid.UUID("44444444-4444-4444-8444-444444444444"), "Soundgarden", "Spoonman"
        )
        self.seed(extra_tracks=[extra])
        self.write_mapping(base_mapping())
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 2)
        after = self.read_tracks()
        self.assertEqual(after[TRACK_TOOL].artist, "TOOL")
        self.assertFalse(self.report_path.exists())

    def test_prefix_matching_two_tracks_aborts(self) -> None:
        extra = seed_track(
            uuid.UUID("22222222-9999-4999-8999-999999999999"), "Pearl Jam", "Alive"
        )
        self.seed(extra_tracks=[extra])
        self.write_mapping(base_mapping())
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 2)
        after = self.read_tracks()
        self.assertEqual(after[TRACK_MOJIBAKE].title, MOJIBAKE_TITLE)
        self.assertFalse(self.report_path.exists())

    def test_delete_sets_deleted_at_only_on_that_row(self) -> None:
        self.seed()
        mapping = base_mapping()
        mapping["tracks"][2] = {
            "id": TRACK_TOOL.hex[:8],
            "expect_id_prefix": TRACK_TOOL.hex[:8],
            "expect": {"artist": "TOOL", "title": "The Pot"},
            "action": "delete",
            "note": "duplicate of another pressing",
        }
        self.write_mapping(mapping)
        before = self.read_tracks()
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 0)
        after = self.read_tracks()
        self.assertIsNotNone(after[TRACK_TOOL].deleted_at)
        # Soft delete touches nothing else on the row.
        self.assertEqual(after[TRACK_TOOL].artist, "TOOL")
        self.assertEqual(after[TRACK_TOOL].title, "The Pot")
        for track_id in (TRACK_FULL, TRACK_MOJIBAKE):
            self.assertIsNone(after[track_id].deleted_at)
        # The previously soft-deleted row keeps its original deleted_at.
        self.assertEqual(after[TRACK_DELETED].deleted_at, before[TRACK_DELETED].deleted_at)
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["counts"]["deleted"], 1)
        self.assertEqual(report["counts"]["tracks_updated"], 2)
        self.assertEqual(
            [row["id"] for row in report["deletions"]], [str(TRACK_TOOL)]
        )
        self.assertNotIn(str(TRACK_TOOL), {row["id"] for row in report["tracks"]})

    def test_delete_expect_mismatch_aborts_with_exit_2(self) -> None:
        self.seed()
        mapping = base_mapping()
        mapping["tracks"][2] = {
            "id": TRACK_TOOL.hex[:8],
            "expect_id_prefix": TRACK_TOOL.hex[:8],
            "expect": {"artist": "TOOL", "title": "Wrong Title"},
            "action": "delete",
        }
        self.write_mapping(mapping)
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 2)
        after = self.read_tracks()
        self.assertIsNone(after[TRACK_TOOL].deleted_at)
        self.assertEqual(after[TRACK_FULL].artist, "")
        self.assertFalse(self.report_path.exists())

    def test_dry_run_deletes_nothing(self) -> None:
        self.seed()
        mapping = base_mapping()
        mapping["tracks"][2] = {
            "id": TRACK_TOOL.hex[:8],
            "expect_id_prefix": TRACK_TOOL.hex[:8],
            "expect": {"artist": "TOOL", "title": "The Pot"},
            "action": "delete",
            "note": "duplicate of another pressing",
        }
        self.write_mapping(mapping)
        code = self.run_cli()
        self.assertEqual(code, 0)
        after = self.read_tracks()
        for track in after.values():
            if track.id == TRACK_DELETED:
                continue
            self.assertIsNone(track.deleted_at)
        self.assertFalse(self.report_path.exists())

    def test_report_written_on_apply(self) -> None:
        self.seed()
        self.write_mapping(base_mapping())
        code = self.run_cli("--apply", "--report", str(self.report_path))
        self.assertEqual(code, 0)
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["schema"], "shizzle-track-metadata-fix-v1-run")
        self.assertIn("applied_at", report)
        self.assertIn("database_host", report)
        self.assertEqual(report["counts"]["mapping_entries"], 3)
        self.assertEqual(report["counts"]["tracks_updated"], 3)
        self.assertEqual(report["counts"]["tracks_unchanged"], 0)
        by_id = {row["id"]: row for row in report["tracks"]}
        self.assertEqual(
            by_id[str(TRACK_MOJIBAKE)]["before"],
            {"artist": "Pearl Jam", "title": MOJIBAKE_TITLE},
        )
        self.assertEqual(
            by_id[str(TRACK_MOJIBAKE)]["after"],
            {"artist": "Pearl Jam", "title": "Black (Unplugged)"},
        )
        self.assertEqual(by_id[str(TRACK_MOJIBAKE)]["note"], "source title has mojibake; match on id prefix")
        # The report must never carry credentials.
        self.assertNotIn(self.database_url, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
