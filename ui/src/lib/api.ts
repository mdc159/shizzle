/**
 * API Client for Local Karaoke Stem Splitter
 *
 * Communicates with local FastAPI backend for:
 * - File upload and processing
 * - Library browsing
 * - Job status polling
 * - Stem manifest loading
 */

import type { Track, StemsManifest, JobStatus, LibraryResponse } from '@/types/karaoke';

/**
 * Upload a video file for stem separation
 */
export async function uploadFile(file: File): Promise<{ jobId: string }> {
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch('/api/upload', {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    throw new Error(`Upload failed: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Fetch library of completed tracks
 */
export async function getLibrary(): Promise<Track[]> {
  const response = await fetch('/api/library');

  if (!response.ok) {
    throw new Error(`Failed to fetch library: ${response.statusText}`);
  }

  const data: LibraryResponse = await response.json();
  return data.tracks;
}

/**
 * Poll job status during processing
 */
export async function getJobStatus(jobId: string): Promise<JobStatus> {
  const response = await fetch(`/api/jobs/${encodeURIComponent(jobId)}`);

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error('Job not found');
    }
    throw new Error(`Failed to get job status: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Load stems manifest for a track
 * @param slug - Track slug (job ID)
 * @param baseUrl - Base URL for the track files (e.g., /api/tracks/{id})
 */
export async function loadManifest(slug: string, baseUrl?: string): Promise<StemsManifest> {
  const manifestUrl = baseUrl
    ? `${baseUrl}/stems.json`
    : `/api/tracks/${slug}/stems.json`;

  const response = await fetch(manifestUrl);

  if (!response.ok) {
    throw new Error(`Failed to load manifest: ${response.statusText}`);
  }

  return response.json();
}
