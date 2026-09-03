/**
 * Parse a source file name into artist/title metadata for the upload dialog
 * prefill. Mirrors the control plane's source-name parser
 * (library/src/shizzle_server/metadata.py) step for step, pinned by
 * e2e/fixtures/source_titles.json — a verbatim copy of
 * library/tests/fixtures/source_titles.json (backend is source of truth).
 * Words are never re-cased.
 *
 * Rules: NFC-normalize and collapse whitespace; strip a trailing KNOWN media
 * extension (an arbitrary ".<suffix>" like ".v1" or ".2025" is part of the
 * title); drop bracketed segments whose every token is upload junk (empty
 * brackets count as junk) while keeping bracketed recording info (live,
 * acoustic, cover, venue/year, ...); split on the first depth-0 " - " (or
 * en/em dash equivalent) — separators inside brackets do not split; strip
 * trailing junk fragments from the title (" - HD", ", Full HD", and
 * whitespace-delimited multi-word phrases like "Official Music Video"),
 * then dangling separators; move a with/feat./featuring/ft. guest clause
 * from the artist into the title as a parenthetical.
 */

// Tokens that only describe the upload, never the recording (verbatim copy of
// metadata._JUNK_TOKENS). "and"/"&" are joiners so "(Remaster, Resync and
// Upscale)" counts as junk.
const JUNK_TOKENS = new Set([
  'official',
  'video',
  'videos',
  'audio',
  'music',
  'lyric',
  'lyrics',
  'visualizer',
  'visualiser',
  'hd',
  'full',
  '4k',
  '8k',
  '1080p',
  '720p',
  '2160p',
  'remaster',
  'remastered',
  'resync',
  'upscale',
  'and',
  '&',
]);

// Trailing file extensions stripped before anything else (verbatim copy of
// metadata._FILE_EXTENSIONS).
const FILE_EXTENSIONS = new Set([
  'mp4',
  'mp3',
  'mkv',
  'avi',
  'mov',
  'webm',
  'm4v',
  'm4a',
  'wav',
  'flac',
  'mpg',
  'mpeg',
  'wmv',
  'ogg',
]);

const BRACKET_RE = /\([^()]*\)|\[[^\][]*\]/g;

// Trailing " - HD" / ", Full HD" style fragments outside any brackets
// (verbatim port of metadata._TRAILING_JUNK_RE).
const TRAILING_JUNK_RE =
  /(?:\s*[-–—,]\s*(?:official(?:\s+(?:music|lyric))?\s+(?:video|audio)|full\s+hd|hd|4k|8k|1080p|720p|2160p|remaster(?:ed)?|resync|upscale|visuali[sz]er|lyrics?|videos?|audio))+\s*$/i;

// Multi-word junk phrases are also stripped with a plain-space delimiter
// ("Song Official Music Video"). Single tokens are not: a real title may
// legitimately end in "Video" or "Audio" (metadata._TRAILING_JUNK_PHRASE_RE).
const TRAILING_JUNK_PHRASE_RE =
  /(?:\s+(?:official\s+(?:music|lyric)\s+video|official\s+(?:video|audio)|(?:music|lyric)\s+video|full\s+hd))+\s*$/i;

// Characters stripped from the ends of a cleaned fragment: whitespace, the
// three separator dashes, and comma (Python str.strip(" -–—,")).
const EDGE_CHARS_RE_START = /^[\s\-–—,]+/;
const EDGE_CHARS_RE_END = /[\s\-–—,]+$/;

// Whitespace, backend-compatible: Python's str.split()/str.strip() (the
// control plane's _nfc_collapse) also treat U+001C–U+001F and U+0085 (NEL)
// as whitespace where JS \s does not. Collapsing with plain \s would leave
// those characters in the prefill while the backend parse drops them. The
// control-character range is intentional (no-control-regex).
// eslint-disable-next-line no-control-regex
const WHITESPACE_RUN_RE = /[\s\u0085\u001C-\u001F]+/g;
// eslint-disable-next-line no-control-regex
const SPACE_BEFORE_COMMA_RE = /[\s\u0085\u001C-\u001F]+,/g;

function stripEdge(text: string): string {
  return text.trim().replace(EDGE_CHARS_RE_START, '').replace(EDGE_CHARS_RE_END, '').trim();
}

function stripExtension(name: string): string {
  const dot = name.lastIndexOf('.');
  if (dot > 0 && FILE_EXTENSIONS.has(name.slice(dot + 1).toLowerCase())) {
    return name.slice(0, dot);
  }
  return name;
}

/**
 * True when a bracketed segment says nothing about the recording. Empty
 * brackets count as junk (dangling "()" / "[]"). Any token outside the junk
 * set — live, unplugged, acoustic, cover, session, a year, a venue — keeps
 * the segment.
 */
function isJunkFragment(inner: string): boolean {
  const tokens = inner
    .trim()
    .toLowerCase()
    .split(/[\s,]+/)
    .filter(Boolean)
    .map((token) => token.replace(/^[.,]+|[.,]+$/g, ''));
  return tokens.every((token) => JUNK_TOKENS.has(token));
}

/**
 * Index of the first separator dash (" - ", " – ", " — ") outside any
 * brackets, or -1. Bracketed separators ("(Philadelphia, PA - May 23, 2025)")
 * never split.
 */
function findSeparator(name: string): number {
  let depth = 0;
  for (let i = 0; i < name.length; i++) {
    const ch = name[i];
    if (ch === '(' || ch === '[') {
      depth++;
    } else if (ch === ')' || ch === ']') {
      depth = Math.max(0, depth - 1);
    } else if (
      (ch === '-' || ch === '–' || ch === '—') &&
      depth === 0 &&
      name[i - 1] === ' ' &&
      name[i + 1] === ' '
    ) {
      return i;
    }
  }
  return -1;
}

/** Drop trailing junk fragments and dangling separators. */
function cleanTitleTail(title: string): string {
  let result = title;
  for (;;) {
    const next = result.replace(TRAILING_JUNK_RE, '').replace(TRAILING_JUNK_PHRASE_RE, '');
    if (next === result) break;
    result = next;
  }
  return stripEdge(result);
}

export function parseSourceTitle(raw: string): {
  artist: string;
  title: string;
  confident: boolean;
} {
  // NFC-normalize and collapse whitespace; strip a trailing known extension.
  let name = stripExtension(raw.normalize('NFC').replace(WHITESPACE_RUN_RE, ' ').trim());

  // Drop bracketed/parenthesised upload junk; keep recording info brackets.
  name = name.replace(BRACKET_RE, (segment) =>
    isJunkFragment(segment.slice(1, -1)) ? '' : segment
  );
  // "Title , Full HD" -> "Title, Full HD"; collapse and strip edge " ,".
  name = name
    .replace(SPACE_BEFORE_COMMA_RE, ',')
    .replace(WHITESPACE_RUN_RE, ' ')
    .trim()
    .replace(/^[ ,]+|[ ,]+$/g, '');

  // Split on the FIRST depth-0 spaced hyphen / en dash / em dash.
  const separatorIndex = findSeparator(name);
  if (separatorIndex === -1) {
    return { artist: '', title: cleanTitleTail(name), confident: false };
  }

  let artist = name.slice(0, separatorIndex).trim();
  let title = cleanTitleTail(name.slice(separatorIndex + 1));

  // Move a with/feat./featuring/ft. guest clause from the artist into the
  // title (the guest belongs to the recording, not the artist field).
  const guest = /^(.*?)\s+(with|feat\.?|featuring|ft\.?)\s+(.+)$/i.exec(artist);
  if (guest) {
    artist = guest[1];
    if (title) {
      title = `${title} (${guest[2]} ${guest[3]})`;
    }
  }
  artist = stripEdge(artist);

  if (!artist || !title) {
    return { artist: '', title, confident: false };
  }
  return { artist, title, confident: true };
}
