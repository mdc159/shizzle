/**
 * Search/sort view helpers for the Library drawer.
 *
 * Search folds case and diacritics on both sides so typing "cafe" matches
 * "Café". Sorting treats duration <= 0 as "unknown" (the backend stores 0.0
 * when no duration is known) and always places unknown durations last.
 */

import type { Track } from '@/types/karaoke';

export type LibrarySort =
  | 'newest'
  | 'title-asc'
  | 'title-desc'
  | 'artist-asc'
  | 'duration-asc'
  | 'duration-desc';

export const LIBRARY_SORT_OPTIONS: ReadonlyArray<{ value: LibrarySort; label: string }> = [
  { value: 'newest', label: 'Sort: Newest' },
  { value: 'title-asc', label: 'Sort: Title (A-Z)' },
  { value: 'title-desc', label: 'Sort: Title (Z-A)' },
  { value: 'artist-asc', label: 'Sort: Artist (A-Z)' },
  { value: 'duration-asc', label: 'Sort: Duration (Shortest)' },
  { value: 'duration-desc', label: 'Sort: Duration (Longest)' },
];

export const LIBRARY_SORT_STORAGE_KEY = 'shizzle_library_sort';

/** Trim, lowercase, and strip diacritics (NFD + combining-mark removal). */
export function normalizeForSearch(s: string): string {
  return s.trim().toLowerCase().normalize('NFD').replace(/\p{M}/gu, '');
}

/**
 * Substring match on normalized title OR artist. An empty normalized query
 * returns the same array reference so callers can rely on referential
 * stability for memoization.
 */
export function filterTracks(tracks: Track[], query: string): Track[] {
  const normalizedQuery = normalizeForSearch(query);
  if (normalizedQuery.length === 0) return tracks;
  return tracks.filter(
    (track) =>
      normalizeForSearch(track.title).includes(normalizedQuery) ||
      normalizeForSearch(track.artist).includes(normalizedQuery)
  );
}

const compareLocale = (a: string, b: string): number =>
  a.localeCompare(b, undefined, { sensitivity: 'base' });

// Unknown durations (<= 0) always sort last, for both directions. Array
// sort is stable, so ties keep the incoming (API recency) order.
const compareDuration = (a: Track, b: Track, direction: 1 | -1): number => {
  const aKnown = a.duration > 0;
  const bKnown = b.duration > 0;
  if (aKnown && bKnown) return (a.duration - b.duration) * direction;
  if (aKnown) return -1;
  if (bKnown) return 1;
  return 0;
};

/** 'newest' returns the input reference (API order is recency order). */
export function sortTracks(tracks: Track[], sort: LibrarySort): Track[] {
  if (sort === 'newest') return tracks;
  const sorted = [...tracks];
  sorted.sort((a, b) => {
    switch (sort) {
      case 'title-asc':
        return compareLocale(a.title, b.title);
      case 'title-desc':
        return compareLocale(b.title, a.title);
      case 'artist-asc':
        return compareLocale(a.artist, b.artist);
      case 'duration-asc':
        return compareDuration(a, b, 1);
      case 'duration-desc':
        return compareDuration(a, b, -1);
    }
  });
  return sorted;
}

export function isLibrarySort(v: unknown): v is LibrarySort {
  return typeof v === 'string' && LIBRARY_SORT_OPTIONS.some((option) => option.value === v);
}
