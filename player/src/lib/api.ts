/**
 * API Client for the Shizzle control plane.
 *
 * - Passcode auth (POST /api/auth) → bearer token on every call (see auth.ts).
 * - Library, job status, upload.
 * - Playback manifest: GET /api/tracks/{id}/manifest returns a manifest whose
 *   `video` / `stems[].file` are same-origin `/cdn` paths (cloud tracks) that
 *   Caddy proxies to CloudFront behind signed cookies, or relative paths
 *   (local profile). The player resolves both via resolveMediaUrl().
 */

import type { Track, StemsManifest, JobStatus, JobEvent, LibraryResponse } from '@/types/karaoke';
import { authFetch, setToken } from '@/lib/auth';

interface AuthResult {
  ok: boolean;
  mediaCookies: boolean;
}

/**
 * Exchange the shared passcode for a device token (+ CloudFront media cookies).
 * Returns ok=false on a wrong passcode.
 */
export async function login(passcode: string): Promise<AuthResult> {
  const response = await fetch('/api/auth', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ passcode }),
  });
  if (response.status === 401) return { ok: false, mediaCookies: false };
  if (!response.ok) throw new Error(`Auth failed: ${response.statusText}`);
  const data = await response.json();
  setToken(data.token);
  return { ok: true, mediaCookies: !!data.mediaCookies };
}

/**
 * Refresh the CloudFront signed cookies for this device. Called on app mount
 * so a returning (still-tokened) device re-arms media access.
 */
export async function refreshMediaSession(): Promise<{ cloudfront: boolean }> {
  const response = await authFetch('/api/media/session', { method: 'POST' });
  if (!response.ok) throw new Error(`Media session failed: ${response.statusText}`);
  return response.json();
}

/** Optional clean metadata that overrides the server-side name parsing. */
interface SourceMetadata {
  title?: string;
  artist?: string;
}

/**
 * Upload a video file for stem separation. Optional title/artist ride along
 * as multipart fields so the track lands in the library with clean metadata.
 */
export async function uploadFile(file: File, metadata?: SourceMetadata): Promise<{ jobId: string }> {
  const formData = new FormData();
  formData.append('file', file);
  const title = metadata?.title?.trim();
  const artist = metadata?.artist?.trim();
  if (title) formData.append('title', title);
  if (artist) formData.append('artist', artist);

  const response = await authFetch('/api/upload', {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    throw new Error(`Upload failed: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Fetch library of completed tracks.
 */
export async function getLibrary(): Promise<Track[]> {
  const response = await authFetch('/api/library');

  if (!response.ok) {
    throw new Error(`Failed to fetch library: ${response.statusText}`);
  }

  const data: LibraryResponse = await response.json();
  return data.tracks;
}

/** Fetch recent pipeline jobs. */
export async function getJobs(signal?: AbortSignal): Promise<JobStatus[]> {
  const response = await authFetch('/api/jobs', { signal });
  if (!response.ok) throw new Error(`Failed to fetch jobs: ${response.statusText}`);
  return (await response.json()).jobs;
}

/** Fetch one job's append-only event history. */
export async function getJobEvents(jobId: string): Promise<JobEvent[]> {
  const response = await authFetch(`/api/jobs/${encodeURIComponent(jobId)}/events`);
  if (!response.ok) throw new Error(response.status === 404 ? 'Job not found' : `Failed to fetch job events: ${response.statusText}`);
  return (await response.json()).events;
}

/**
 * Poll job status during processing.
 */
export async function getJobStatus(jobId: string): Promise<JobStatus> {
  const response = await authFetch(`/api/jobs/${encodeURIComponent(jobId)}`);

  if (!response.ok) {
    if (response.status === 404) {
      throw new Error('Job not found');
    }
    throw new Error(`Failed to get job status: ${response.statusText}`);
  }

  return response.json();
}

/**
 * Load the playback manifest for a track. The server resolves media refs to
 * fetchable URLs (same-origin `/cdn` for cloud tracks, relative for local).
 * @param trackId - Track id (slug).
 */
export async function loadManifest(trackId: string): Promise<StemsManifest> {
  const response = await authFetch(`/api/tracks/${encodeURIComponent(trackId)}/manifest`);

  if (!response.ok) {
    throw new Error(`Failed to load manifest: ${response.statusText}`);
  }

  return response.json();
}
