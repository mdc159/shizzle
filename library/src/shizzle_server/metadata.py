"""Clean artist/title metadata parsed from raw source names at ingest.

Raw YouTube/file titles arrive as "Artist - Title (Official Music Video)".
This module splits artist from title and strips platform junk while keeping
parenthetical content that changes the recording (live, unplugged, cover,
session, version, guest performers, tour/venue/year information). It never
re-cases words: only whitespace normalization and junk removal.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class ParsedTitle:
    artist: str
    title: str
    confident: bool


# Tokens that only describe the upload, never the recording. A bracketed
# segment or trailing dash/comma fragment composed solely of these is dropped.
# "and"/"&" are joiners so "(Remaster, Resync and Upscale)" counts as junk.
_JUNK_TOKENS = {
    "official",
    "video",
    "videos",
    "audio",
    "music",
    "lyric",
    "lyrics",
    "visualizer",
    "visualiser",
    "hd",
    "full",
    "4k",
    "8k",
    "1080p",
    "720p",
    "2160p",
    "remaster",
    "remastered",
    "resync",
    "upscale",
    "and",
    "&",
}

# Trailing file extensions stripped before anything else.
_FILE_EXTENSIONS = {
    "mp4",
    "mp3",
    "mkv",
    "avi",
    "mov",
    "webm",
    "m4v",
    "m4a",
    "wav",
    "flac",
    "mpg",
    "mpeg",
    "wmv",
    "ogg",
}

# Artist/title separators, in the order they are tried at each position.
_SEPARATORS = (" - ", " – ", " — ")

# Guest-performer markers inside the artist portion ("AC DC with Steven
# Tyler - ..."): the guest belongs to the title, not the artist field.
_GUEST_RE = re.compile(r"\s+(with|feat\.?|featuring|ft\.?)\s+", re.IGNORECASE)

_BRACKET_RE = re.compile(r"\([^()]*\)|\[[^\][]*\]")

# Trailing " - HD" / ", Full HD" style fragments outside any brackets.
_TRAILING_JUNK_RE = re.compile(
    r"(?:\s*[-–—,]\s*(?:"
    r"official(?:\s+(?:music|lyric))?\s+(?:video|audio)"
    r"|full\s+hd|hd|4k|8k|1080p|720p|2160p"
    r"|remaster(?:ed)?|resync|upscale"
    r"|visuali[sz]er|lyrics?|videos?|audio"
    r"))+\s*$",
    re.IGNORECASE,
)

# Multi-word junk phrases are also stripped with a plain-space delimiter
# ("Song Official Music Video"). Single tokens are not: a real title may
# legitimately end in "Video" or "Audio", so those still need a dash/comma.
_TRAILING_JUNK_PHRASE_RE = re.compile(
    r"(?:\s+(?:"
    r"official\s+(?:music|lyric)\s+video"
    r"|official\s+(?:video|audio)"
    r"|(?:music|lyric)\s+video"
    r"|full\s+hd"
    r"))+\s*$",
    re.IGNORECASE,
)

_SPACE_BEFORE_COMMA_RE = re.compile(r"\s+,")


def _nfc_collapse(text: str) -> str:
    return " ".join(unicodedata.normalize("NFC", text).split())


def _strip_extension(text: str) -> str:
    stem, dot, ext = text.rpartition(".")
    if dot and stem and ext.lower() in _FILE_EXTENSIONS:
        return stem
    return text


def _is_junk_fragment(inner: str) -> bool:
    """True when a bracketed segment says nothing about the recording.

    Empty brackets count as junk (dangling "()" / "[]"). Any token outside
    the junk set — live, unplugged, acoustic, acústico, cover, session(s),
    version, feat./with/ft., induction, a year, a venue — keeps the segment.
    """
    tokens = [t.strip(".,") for t in re.split(r"[\s,]+", inner.strip().lower()) if t]
    return all(t in _JUNK_TOKENS for t in tokens)


def _drop_junk_brackets(text: str) -> str:
    def repl(match: re.Match[str]) -> str:
        segment = match.group(0)
        return "" if _is_junk_fragment(segment[1:-1]) else segment

    return _BRACKET_RE.sub(repl, text)


def _split_artist_title(text: str) -> tuple[str, str] | None:
    """Split on the first top-level separator (never one inside brackets)."""
    depth = 0
    for i, ch in enumerate(text):
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth = max(0, depth - 1)
        elif depth == 0:
            for sep in _SEPARATORS:
                if text.startswith(sep, i):
                    return text[:i], text[i + len(sep) :]
    return None


def _clean_title_tail(title: str) -> str:
    """Drop trailing junk fragments and dangling separators."""
    previous = None
    while previous != title:
        previous = title
        title = _TRAILING_JUNK_RE.sub("", title)
        title = _TRAILING_JUNK_PHRASE_RE.sub("", title)
    return title.strip().strip(" -–—,").strip()


def parse_source_title(raw: str) -> ParsedTitle:
    """Parse a raw source name (YouTube title or filename) into artist/title.

    ``confident`` is False when no artist/title separator exists, meaning a
    human should look at the row.
    """
    text = _strip_extension(_nfc_collapse(raw))
    text = _drop_junk_brackets(text)
    text = _SPACE_BEFORE_COMMA_RE.sub(",", _nfc_collapse(text)).strip(" ,")

    split = _split_artist_title(text)
    if split is None:
        return ParsedTitle(artist="", title=_clean_title_tail(text), confident=False)

    artist_part, title_part = split
    title = _clean_title_tail(title_part)

    artist = artist_part
    guest = _GUEST_RE.search(artist_part)
    if guest is not None:
        guest_clause = artist_part[guest.start() :].strip()
        artist = artist_part[: guest.start()]
        if title:
            title = f"{title} ({guest_clause})"
    artist = artist.strip(" -–—,")

    if not artist or not title:
        return ParsedTitle(artist="", title=title, confident=False)
    return ParsedTitle(artist=artist, title=title, confident=True)


def normalize_artist(raw: str) -> str:
    """NFC + whitespace collapse only; never re-case."""
    return _nfc_collapse(raw)


def resolve_track_metadata(
    supplied_title: str | None,
    supplied_artist: str | None,
    fallback_source: str | None,
) -> ParsedTitle:
    """Resolve the final artist/title for a job at ingest.

    - User supplied both title and artist: use them verbatim (whitespace
      normalized) — the user knows better than the parser.
    - User supplied a title but no artist: parse the supplied title.
    - No title: parse the fallback source name (filename stem for uploads).
      A user-supplied artist still wins over the parsed one.
    """
    artist = normalize_artist(supplied_artist) if supplied_artist else ""
    if supplied_title and supplied_title.strip():
        if artist:
            return ParsedTitle(artist=artist, title=_nfc_collapse(supplied_title), confident=True)
        return parse_source_title(supplied_title)
    parsed = parse_source_title(fallback_source or "")
    if artist:
        return ParsedTitle(artist=artist, title=parsed.title, confident=True)
    return parsed
