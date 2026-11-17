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

import type {
  Track,
  StemsManifest,
  JobStatus,
  JobEvent,
  LibraryResponse,
  NodeInfo,
  NodeListResponse,
  AdminJobsResponse,
  AdminJobDetail,
  AdminNode,
  AdminSettings,
} from '@/types/karaoke';
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
 * Submit a video URL for download and stem separation by a worker node
 * (docs/WORKER-NODES.md). Title/artist are optional: the node fills them
 * from the video's metadata when omitted.
 */
export async function submitUrl(
  url: string,
  metadata?: SourceMetadata
): Promise<{ jobId: string; maxWaitSeconds?: number }> {
  const body: Record<string, string> = { url: url.trim() };
  const title = metadata?.title?.trim();
  const artist = metadata?.artist?.trim();
  if (title) body.title = title;
  if (artist) body.artist = artist;

  const response = await authFetch('/api/submit-url', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });

  if (!response.ok) {
    let detail = response.statusText;
    try {
      const payload = await response.json();
      if (typeof payload?.detail === 'string') detail = payload.detail;
      // A 422 carries a list of field errors; surface the first one.
      else if (Array.isArray(payload?.detail) && payload.detail.length > 0) {
        const first = payload.detail[0];
        const field = Array.isArray(first?.loc) ? first.loc[first.loc.length - 1] : undefined;
        if (typeof first?.msg === 'string') detail = field ? `${field}: ${first.msg}` : first.msg;
      }
    } catch {
      // keep statusText
    }
    throw new Error(`Submit failed: ${detail}`);
  }

  return response.json();
}

/** Fetch the worker-node registry for the pipeline dashboard. */
export async function getNodes(signal?: AbortSignal): Promise<NodeInfo[]> {
  const response = await authFetch('/api/nodes', { signal });
  if (!response.ok) throw new Error(`Failed to fetch nodes: ${response.statusText}`);
  const data: Partial<NodeListResponse> = await response.json();
  // A malformed body is a failure, not an empty registry: the panel records
  // it as "node status unavailable" rather than showing "no nodes".
  if (!Array.isArray(data.nodes)) throw new Error('Failed to fetch nodes: unexpected response');
  return data.nodes;
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
export async function loadManifest(trackId: string, signal?: AbortSignal): Promise<StemsManifest> {
  const response = await authFetch(`/api/tracks/${encodeURIComponent(trackId)}/manifest`, { signal });

  if (!response.ok) {
    throw new Error(`Failed to load manifest: ${response.statusText}`);
  }

  return response.json();
}

// --- Administrator surface (/admin; docs/INVARIANTS.md G) ----------------------
// The admin API is reachable only through the owner's Tailscale-only reverse
// proxy, which adds the X-Shizzle-Admin secret header server-side. The browser
// never holds that secret: from the public listener these calls 404 and the
// admin page says so plainly. No passcode token is required either way.

export class AdminUnavailableError extends Error {
  constructor() {
    super('The admin API is not reachable from this network (404). It is only served on the owner\'s Tailscale network.');
    this.name = 'AdminUnavailableError';
  }
}

async function adminFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, init);
  if (response.status === 404) throw new AdminUnavailableError();
  if (!response.ok) throw new Error(`Admin request failed: ${response.statusText}`);
  return response.json() as Promise<T>;
}

export async function getAdminJobs(
  status?: JobStatus['status'],
  signal?: AbortSignal
): Promise<AdminJobsResponse> {
  const query = status ? `?status=${status}` : '';
  return adminFetch<AdminJobsResponse>(`/api/admin/jobs${query}`, { signal });
}

export async function getAdminJobDetail(
  jobId: string,
  verifyS3 = false,
  signal?: AbortSignal
): Promise<AdminJobDetail> {
  const query = verifyS3 ? '?verify=s3' : '';
  return adminFetch<AdminJobDetail>(`/api/admin/jobs/${encodeURIComponent(jobId)}${query}`, { signal });
}

export async function getAdminNodes(signal?: AbortSignal): Promise<{ nodes: AdminNode[] }> {
  return adminFetch<{ nodes: AdminNode[] }>('/api/admin/nodes', { signal });
}

export async function setAdminNodeEnabled(nodeId: string, enabled: boolean): Promise<void> {
  await adminFetch(`/api/admin/nodes/${encodeURIComponent(nodeId)}/${enabled ? 'enable' : 'disable'}`, {
    method: 'POST',
  });
}

export async function getAdminSettings(): Promise<AdminSettings> {
  return adminFetch<AdminSettings>('/api/admin/settings');
}

export async function putAdminSettings(update: {
  nodeRouting?: string | null;
  runpodMaxConcurrent?: number | null;
}): Promise<AdminSettings> {
  return adminFetch<AdminSettings>('/api/admin/settings', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(update),
  });
}

export async function releaseAdminAssignment(assignmentId: string): Promise<void> {
  await adminFetch(`/api/admin/assignments/${encodeURIComponent(assignmentId)}/release`, {
    method: 'POST',
  });
}
