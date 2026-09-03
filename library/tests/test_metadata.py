"""Metadata parser: unit rules plus a table-driven pass over the fixture of
29 real production titles and their curated targets (R1/R2).

The fixture pins exact parser output (`parser_*`), so any behaviour change
is a deliberate fixture update. The curated `expect_*` fields are the human
targets; the parser is allowed to fall short on rows only a human can decide
(missing artist, canonical spelling, reworded version info) — at least 20 of
29 titles must match, and a claimed artist must always match.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from shizzle_server.metadata import (
    ParsedTitle,
    normalize_artist,
    parse_source_title,
    resolve_track_metadata,
)

FIXTURE = Path(__file__).parent / "fixtures" / "source_titles.json"
ROWS = json.loads(FIXTURE.read_text(encoding="utf-8"))["rows"]

# Platform junk asserted absent from every parsed fixture title. Scope: the
# parser strips these tokens from bracketed segments and trailing dash/comma
# fragments only — a bare whitespace-delimited trailing token survives on
# purpose (real titles can end in "Lyrics" or "Video"), so this is a
# fixture-level check on real production names, not a guarantee about
# arbitrary input. Bare "video" / "audio" are deliberately absent: they
# survive only inside a kept segment such as "(Live Video)", where "live"
# marks a different recording.
_JUNK_PATTERNS = [
    r"\bofficial\b",
    r"\bfull\s+hd\b",
    r"\bhd\b",
    r"\b4k\b",
    r"\b8k\b",
    r"\b1080p\b",
    r"\b720p\b",
    r"\b2160p\b",
    r"\bremaster(ed)?\b",
    r"\bresync\b",
    r"\bupscale\b",
    r"\bvisuali[sz]er\b",
    r"\bmusic\s+video\b",
    r"\blyric\s+video\b",
    r"\blyrics?\b",
]


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def _artist_key(text: str) -> str:
    # Case-insensitive and insensitive to the "AC DC" / "AC/DC" slash (and
    # spacing) difference.
    return re.sub(r"[^a-z0-9]", "", text.casefold())


# --- unit rules (R1) ---------------------------------------------------------


def test_nfc_and_whitespace_collapse():
    parsed = parse_source_title("AC DC -  You Shook Me    All Night Long")
    assert parsed.title == "You Shook Me All Night Long"
    # NFC: combining accents compose.
    assert parse_source_title("Beyoncé - Halo".replace("é", "é")).title == "Halo"


def test_strips_file_extension():
    assert parse_source_title("Tool - The Pot.mp4").title == "The Pot"
    assert parse_source_title("Tool - The Pot.MKV").title == "The Pot"
    # "Mr." is not an extension.
    assert parse_source_title("Mr. Brownstone").title == "Mr. Brownstone"


@pytest.mark.parametrize(
    "raw, title",
    [
        ("Van Halen - Panama (Official Music Video)", "Panama"),
        ("Alice In Chains - Rooster [Official Video]", "Rooster"),
        ("AC DC - Hells Bells (Official 4K Video)", "Hells Bells"),
        ("AC DC - T.N.T. (Official Audio)", "T.N.T."),
        ("Band - Song (Official Lyric Video)", "Song"),
        ("Band - Song [HD]", "Song"),
        ("Band - Song (Remastered)", "Song"),
        ("Band - Song [Official Music Video], Full HD (Remaster, Resync and Upscale)", "Song"),
        ("Band - Song (Visualizer)", "Song"),
        ("Band - Song (Lyrics)", "Song"),
        # Whitespace-delimited multi-word junk phrases (no dash or comma).
        ("Band - Song Official Music Video", "Song"),
        ("Band - Song Music Video", "Song"),
        ("Band - Song Official Lyric Video", "Song"),
        ("Band - Song Official Audio", "Song"),
        ("Band - Song Full HD", "Song"),
        # ...but a bare trailing single token is kept: real titles can end
        # in "Video" or "Audio".
        ("Band - Song Video", "Song Video"),
        ("Pearl Jam - Black - Acústico - Unplugged - HD", "Black - Acústico - Unplugged"),
        # Dangling separators and empty brackets are junk too.
        ("Band - Song -", "Song"),
        ("Band - Song ()", "Song"),
        ("Band - Song []", "Song"),
        # Non-media suffixes are part of the title, not an extension.
        ("Band - Song.v1", "Song.v1"),
    ],
)
def test_platform_junk_removed(raw, title):
    assert parse_source_title(raw).title == title


@pytest.mark.parametrize(
    "raw, title",
    [
        ("Band - Song (Live)", "Song (Live)"),
        ("Band - Song (Unplugged)", "Song (Unplugged)"),
        ("Band - Song (Acoustic)", "Song (Acoustic)"),
        ("Band - Song (Acústico)", "Song (Acústico)"),
        ("Band - Song (Black Sabbath cover)", "Song (Black Sabbath cover)"),
        ("Band - Song (Guitar Center Sessions)", "Song (Guitar Center Sessions)"),
        ("AC DC - Shoot To Thrill (Iron Man 2 Version)", "Shoot To Thrill (Iron Man 2 Version)"),
        ("Band - Song (Live Video)", "Song (Live Video)"),
        (
            "Band - Song (Stade De France, Paris, June 2001)",
            "Song (Stade De France, Paris, June 2001)",
        ),
        ("Band - Song 2003 Induction", "Song 2003 Induction"),
        ("Van Halen - (Oh) Pretty Woman (Official Music Video)", "(Oh) Pretty Woman"),
    ],
)
def test_recording_changing_content_kept(raw, title):
    assert parse_source_title(raw).title == title


def test_split_on_first_separator_only():
    parsed = parse_source_title("Temple of the Dog - War Pigs – Live in San Francisco")
    assert parsed.artist == "Temple of the Dog"
    assert parsed.title == "War Pigs – Live in San Francisco"


def test_separator_inside_brackets_does_not_split():
    parsed = parse_source_title("Metallica Orion (Philadelphia, PA - May 23, 2025)")
    assert parsed.artist == ""
    assert parsed.confident is False


def test_en_and_em_dash_separators():
    assert parse_source_title("Tool – The Pot").artist == "Tool"
    assert parse_source_title("Tool — The Pot").artist == "Tool"


def test_no_separator_is_not_confident():
    parsed = parse_source_title("The Pot")
    assert parsed == ParsedTitle(artist="", title="The Pot", confident=False)


def test_guest_performer_moves_from_artist_to_title():
    parsed = parse_source_title("AC DC with Steven Tyler - You Shook Me All Night Long")
    assert parsed.artist == "AC DC"
    assert parsed.title == "You Shook Me All Night Long (with Steven Tyler)"


def test_never_recases_words():
    parsed = parse_source_title("BLACK SABBATH - Into The Void")
    assert parsed.artist == "BLACK SABBATH"
    assert parsed.title == "Into The Void"


def test_normalize_artist_only_normalizes_whitespace():
    assert normalize_artist("  Guns   N' Roses ") == "Guns N' Roses"
    assert normalize_artist("TOOL") == "TOOL"


def test_resolve_prefers_user_artist_and_title():
    parsed = resolve_track_metadata("My Song", "My Artist", "whatever.mp4")
    assert parsed == ParsedTitle(artist="My Artist", title="My Song", confident=True)


def test_resolve_parses_supplied_title_when_artist_missing():
    parsed = resolve_track_metadata("Tool - The Pot (Official Video)", None, None)
    assert parsed.artist == "Tool"
    assert parsed.title == "The Pot"


def test_resolve_falls_back_to_source_name():
    parsed = resolve_track_metadata(None, None, "Soundgarden - Spoonman (Official Video)")
    assert parsed.artist == "Soundgarden"
    assert parsed.title == "Spoonman"
    # A typed artist overrides the parsed one.
    parsed = resolve_track_metadata(None, "Soundgarden", "sg - spoonman")
    assert parsed.artist == "Soundgarden"
    assert parsed.title == "spoonman"


# --- fixture table (R2) ------------------------------------------------------


@pytest.mark.parametrize("row", ROWS, ids=[r["id"][:8] for r in ROWS])
def test_parser_output_is_pinned(row):
    parsed = parse_source_title(row["raw"])
    assert parsed.artist == row["parser_artist"]
    assert parsed.title == row["parser_title"]
    assert parsed.confident == row["parser_confident"]


@pytest.mark.parametrize("row", ROWS, ids=[r["id"][:8] for r in ROWS])
def test_no_junk_tokens_survive(row):
    # Run the parser live: checking the fixture's parser_title field would
    # only re-assert self-authored data and could not catch a parser
    # regression that leaks junk.
    parsed = parse_source_title(row["raw"])
    for pattern in _JUNK_PATTERNS:
        assert not re.search(pattern, parsed.title, re.IGNORECASE), (
            f"{row['id'][:8]}: junk {pattern!r} in {parsed.title!r}"
        )


@pytest.mark.parametrize("row", ROWS, ids=[r["id"][:8] for r in ROWS])
def test_claimed_artist_matches_curated(row):
    if not row["has_separator"]:
        assert row["parser_artist"] == ""
        return
    assert _artist_key(row["parser_artist"]) == _artist_key(row["expect_artist"])


def test_most_titles_match_curated_targets():
    matched = [row for row in ROWS if _squash(row["parser_title"]) == _squash(row["expect_title"])]
    assert len(matched) >= 20, f"only {len(matched)}/{len(ROWS)} titles match; diffs: " + ", ".join(
        r["id"][:8] for r in ROWS if r not in matched
    )
