/**
 * Parse a source file name into artist/title metadata for the upload dialog
 * prefill. Mirrors the control plane's source-name parser so the prefill
 * agrees with what the backend derives when the fields are omitted.
 *
 * Rules: NFC-normalize and collapse whitespace; strip a trailing file
 * extension; drop bracketed upload junk (official video/audio, HD markers,
 * remaster/resync tags, bare "lyrics"/"audio"/"video", and comma/space
 * combinations of those) while keeping bracketed recording info (live,
 * acoustic, cover, feat., venue/year, ...); split on the first " - "
 * (or en/em dash equivalent). Words are never re-cased.
 */

// Single junk phrase. Multi-word alternatives must come first so "official
// music video" and "full hd" win over their single-word parts.
const JUNK_PHRASE =
  '(?:official\\s+(?:music|lyric)\\s+video|official\\s+audio|music\\s+video|full\\s+hd' +
  '|official|hd|4k|1080p|720p|remaster(?:ed)?|resync|upscale|lyrics?|audio|visualizer|video)';

// Bracket content is junk only when it is entirely junk phrases joined by
// spaces and/or commas ("Official Video", "HD, 1080p", "official audio hd").
const JUNK_BRACKET_CONTENT = new RegExp(
  `^${JUNK_PHRASE}(?:[\\s,]+${JUNK_PHRASE})*$`,
  'i'
);

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
    JUNK_BRACKET_CONTENT.test(inner.trim()) ? ' ' : match
  );
  name = name.replace(/\s+/g, ' ').trim();

  // Split on the FIRST spaced hyphen / en dash / em dash.
  const separator = / [-–—] /.exec(name);
  if (!separator) {
    return { artist: '', title: name, confident: false };
  }
  return {
    artist: name.slice(0, separator.index),
    title: name.slice(separator.index + separator[0].length),
    confident: true,
  };
}
