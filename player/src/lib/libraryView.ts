/**
 * Search/sort view helpers for the Library drawer.
 *
 * Search folds case and diacritics on both sides so typing "cafe" matches
 * "Café". Sorting is alphabetical by title or artist; tracks with no
 * artist (empty after trim) always sort last in artist order.
 */

import type { Track } from '@/types/karaoke';

export type LibrarySort = 'newest' | 'title-asc' | 'artist-asc';

export const LIBRARY_SORT_OPTIONS: ReadonlyArray<{ value: LibrarySort; label: string }> = [
  { value: 'newest', label: 'Sort: Newest' },
  { value: 'title-asc', label: 'Sort: Title (A–Z)' },
  { value: 'artist-asc', label: 'Sort: Artist (A–Z)' },
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

// Array sort is stable, so full ties keep the incoming (API recency) order.
const compareTitle = (a: Track, b: Track): number =>
  compareLocale(a.title, b.title) || compareLocale(a.artist, b.artist);

const compareArtist = (a: Track, b: Track): number => {
  const aEmpty = a.artist.trim().length === 0;
  const bEmpty = b.artist.trim().length === 0;
  // Tracks with no artist sort last; among themselves they are ordered by
  // title (full ties keep the incoming API recency order).
  if (aEmpty !== bEmpty) return aEmpty ? 1 : -1;
  return compareLocale(a.artist, b.artist) || compareLocale(a.title, b.title);
};

/** 'newest' returns the input reference (API order is recency order). */
export function sortTracks(tracks: Track[], sort: LibrarySort): Track[] {
  if (sort === 'newest') return tracks;
  const sorted = [...tracks];
  sorted.sort((a, b) => {
    switch (sort) {
      case 'title-asc':
        return compareTitle(a, b);
      case 'artist-asc':
        return compareArtist(a, b);
      default:
        // Unreachable today (LibrarySort is a closed union and 'newest'
        // returned early), but a future variant must not yield undefined
        // from the comparator.
        return 0;
    }
  });
  return sorted;
}

export function isLibrarySort(v: unknown): v is LibrarySort {
  return typeof v === 'string' && LIBRARY_SORT_OPTIONS.some((option) => option.value === v);
}
