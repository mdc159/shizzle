/**
 * Parse a source file name into artist/title metadata for the upload dialog
 * prefill. Mirrors the control plane's source-name parser (pinned by
 * e2e/fixtures/source_titles.json, kept verbatim from
 * library/tests/fixtures/source_titles.json) so the prefill agrees with what
 * the backend derives when the fields are omitted. Words are never re-cased.
 *
 * Rules: NFC-normalize and collapse whitespace; strip a trailing file
 * extension; drop bracketed upload junk (official (music|lyric) video,
 * official audio, HD markers, remaster/resync/upscale tags, bare
 * lyrics/audio/visualizer/video, and comma/space/and combinations of those)
 * while keeping bracketed recording info (live, acoustic, cover, venue/year,
 * ...); split on the first depth-0 " - " (or en/em dash equivalent) —
 * separators inside brackets do not split; strip trailing dash/comma junk
 * fragments from the title (" - HD", ", Full HD"); move a with/feat./ft.
 * guest clause from the artist into the title as a parenthetical.
 */

// Single junk phrase. Multi-word alternatives must come first so "official
// music video" and "full hd" win over their single-word parts.
const JUNK_PHRASE =
  '(?:official\\s+(?:music|lyric)\\s+video|official\\s+audio|music\\s+video|full\\s+hd' +
  '|official|hd|4k|1080p|720p|remaster(?:ed)?|resync|upscale|lyrics?|audio|visualizer|video)';

// Junk fragments are junk phrases joined by spaces, commas, and "and"
// ("Official Video", "HD, 1080p", "Remaster, Resync and Upscale").
const JUNK_FRAGMENT = new RegExp(
  `^${JUNK_PHRASE}(?:(?:[\\s,]+|[\\s,]+and[\\s,]+)${JUNK_PHRASE})*$`,
  'i'
);

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

/** Repeatedly strip trailing dash/comma junk fragments (" - HD", ", Full HD"). */
function stripTrailingJunk(title: string): string {
  let result = title;
  for (;;) {
    const next = result.replace(
      /\s*[-–—,]\s*([^-–—,]+)$/,
      (match, fragment: string) => (JUNK_FRAGMENT.test(fragment.trim()) ? '' : match)
    );
    if (next === result) break;
    result = next;
  }
  return result.trim();
}

export function parseSourceTitle(raw: string): {
  artist: string;
  title: string;
  confident: boolean;
} {
  // NFC-normalize and collapse whitespace.
  let name = raw.normalize('NFC').replace(/\s+/g, ' ').trim();

  // Strip a trailing file extension (.mp4, .mkv, .webm, ...).
  name = name.replace(/\.[a-z0-9]{1,5}$/i, '');

  // Drop bracketed/parenthesised upload junk; keep recording info brackets.
  name = name.replace(/\s*[[(]([^\][()]*)\s*[\])]/g, (match, inner: string) =>
    JUNK_FRAGMENT.test(inner.trim()) ? ' ' : match
  );
  name = name.replace(/\s+/g, ' ').trim();

  // Split on the FIRST depth-0 spaced hyphen / en dash / em dash.
  const separatorIndex = findSeparator(name);
  if (separatorIndex === -1) {
    return { artist: '', title: stripTrailingJunk(name), confident: false };
  }

  let artist = name.slice(0, separatorIndex).trim();
  let title = stripTrailingJunk(name.slice(separatorIndex + 1));

  // Move a with/feat./ft. guest clause from the artist into the title.
  const guest = /^(.*?)\s+(with|feat\.?|ft\.?)\s+(.+)$/i.exec(artist);
  if (guest) {
    artist = guest[1];
    title = `${title} (${guest[2]} ${guest[3]})`;
  }

  return { artist, title, confident: true };
}
