// Stem definitions
export type StemId = 'vocals' | 'drums' | 'bass' | 'guitar' | 'piano' | 'shizzle';

export interface Stem {
  id: StemId;
  name: string;
  file: string;
  /** Default stem gain in dB (manifest v3). Convert via dbToLinear() at load. */
  default_gain_db: number;
}

export interface StemsManifest {
  version?: number;
  track_id?: string;
  generation?: number;
  delivery_profile?: string;
  title: string;
  artist: string;
  duration: number;
  video: string;
  timeline?: {
    start_ms: number;
    duration_ms: number;
    sample_rate_hz: number;
  };
  video_meta?: {
    codec?: string;
    profile?: string;
    pix_fmt?: string;
    avg_frame_rate?: string;
    duration?: number;
  };
  sourceUrl?: string;
  videoId?: string;
  stems: Stem[];
}

export interface Track {
  id: string;
  title: string;
  artist: string;
  slug: string;
  duration: number;
  videoId?: string;
  thumbnailUrl?: string;
  publicUrl: string;
  status?: 'ready' | 'processing' | 'failed';
}

export interface LibraryResponse {
  tracks: Track[];
  total: number;
}

export interface JobStatus {
  jobId: string;
  /** Phase 2 job model (design spec section 4):
   *  pending → downloading → dispatched → splitting → verifying → publishing → ready | failed */
  status:
    | 'pending'
    | 'downloading'
    | 'dispatched'
    | 'splitting'
    | 'verifying'
    | 'publishing'
    | 'ready'
    | 'failed';
  title?: string;
  sourceType?: string;
  progress?: number;
  message?: string;
  error?: string;
  /** Structured machine-readable failure code (e.g. YTDLP_BLOCKED). */
  errorCode?: string;
  attempt?: number;
  stageTimings?: Record<string, number>;
  workerPhase?: string;
  workerHeartbeatAt?: string;
  /** Who holds the current attempt: "runpod" or "node:<node-id>" (docs/WORKER-NODES.md). */
  executor?: string | null;
  trackId?: string;
  createdAt?: string;
  updatedAt?: string;
}

export interface JobEvent {
  event: string;
  detail?: Record<string, unknown>;
  createdAt: string;
}

/** A worker node as listed by GET /api/nodes (docs/WORKER-NODES.md). */
export interface NodeInfo {
  id: string;
  displayName: string;
  online: boolean;
  disabled: boolean;
  capabilities: Record<string, unknown>;
  agentVersion?: string | null;
  lastSeenAt?: string | null;
  busyAssignmentId?: string | null;
}

export interface NodeListResponse {
  nodes: NodeInfo[];
  onlineSeconds: number;
}

// --- Administrator surface (/admin; GET/PUT /api/admin/*) ---------------------

export type AdminJobFlag =
  | 'ready_without_track'
  | 'open_assignment_on_terminal_job'
  | 'node_busy_pointer_stale'
  | 'unresolved_dispatch_reservation'
  | 'assignment_ack_overdue'
  | 'assignment_stalled'
  | 'assignment_over_cap'
  | 'expired_lease_on_runnable'
  | 'wait_over_window'
  | 'stage_stalled';

export interface AdminJobRow {
  jobId: string;
  status: JobStatus['status'];
  stage: string;
  sourceType: string;
  title?: string | null;
  artist?: string;
  attempt: number;
  executor?: string | null;
  runpodJobId?: string | null;
  workerPhase?: string | null;
  workerHeartbeatAt?: string | null;
  errorCode?: string | null;
  errorDetail?: string | null;
  leaseOwner?: string | null;
  leaseExpiresAt?: string | null;
  nextRetryAt?: string | null;
  dismissedAt?: string | null;
  trackId?: string | null;
  createdAt?: string | null;
  updatedAt?: string | null;
  flags: AdminJobFlag[];
}

export interface AdminJobsResponse {
  jobs: AdminJobRow[];
  total: number;
  limit: number;
  offset: number;
  counts: Record<string, number>;
  dismissedCount: number;
}

export type EvidenceStatus = 'ok' | 'missing' | 'mismatch' | 'not_yet' | 'unknown';

export interface EvidenceItem {
  stage: string;
  name: string;
  expected: string;
  observed: string;
  proof: string;
  checkedAt: string;
  status: EvidenceStatus;
}

export interface AdminAssignment {
  id: string;
  nodeId: string;
  kind: string;
  status: string;
  idempotencyKey?: string | null;
  phase?: string | null;
  heartbeatAt?: string | null;
  detail?: Record<string, unknown> | null;
  createdAt: string;
  updatedAt: string;
}

export interface AdminEvent {
  id: number;
  event: string;
  detail?: Record<string, unknown> | null;
  createdAt: string;
}

export interface AdminJobColumns {
  jobId: string;
  sourceType: string;
  title?: string | null;
  artist?: string;
  status: JobStatus['status'];
  runpodJobId?: string | null;
  executor?: string | null;
  workerPhase?: string | null;
  workerHeartbeatAt?: string | null;
  attempt: number;
  idempotencyKey: string;
  leaseOwner?: string | null;
  leaseExpiresAt?: string | null;
  nextRetryAt?: string | null;
  dismissedAt?: string | null;
  profileVersion: number;
  inputChecksum?: string | null;
  outputChecksums?: Record<string, unknown> | null;
  errorCode?: string | null;
  errorDetail?: string | null;
  stageTimings?: Record<string, unknown> | null;
  trackId?: string | null;
  createdAt: string;
  updatedAt: string;
}

export interface AdminTrack {
  id: string;
  title: string;
  artist: string;
  durationSeconds: number;
  s3Prefix: string;
  generation: number;
  manifestKey: string;
  integrity?: Record<string, unknown> | null;
  deletedAt?: string | null;
  createdAt: string;
}

export interface AdminJobDetail {
  job: AdminJobColumns;
  derivedTrackId: string;
  dispatchAttempt: {
    idempotencyKey: string | null;
    attemptPrefix?: string | null;
    runpodJobId?: string | null;
  };
  events: AdminEvent[];
  assignments: AdminAssignment[];
  track?: AdminTrack | null;
  evidence: EvidenceItem[];
}

export interface AdminRetryResponse {
  status: string;
  resumeStage: string;
  attempt: number;
  action?: 'retried' | 'recovered' | 'resubmitted';
  newJobId?: string | null;
}

export interface AdminJobActionResponse {
  ok: boolean;
  status: string;
  dismissedAt?: string | null;
}

export interface AdminBulkResult {
  jobId: string;
  ok: boolean;
  reason?: string | null;
  resumeStage?: string | null;
  action?: 'retried' | 'recovered' | 'resubmitted';
  newJobId?: string | null;
}

export interface AdminLibraryTrack {
  id: string;
  title: string;
  artist: string;
  durationSeconds: number;
  generation: number;
  createdAt: string;
  deletedAt?: string | null;
  jobId?: string | null;
}

export interface AdminTrackActionResponse {
  ok: boolean;
  deletedAt?: string | null;
}

export interface AdminDuplicateMember {
  id: string;
  kind: 'job' | 'track';
  status: string;
  title?: string | null;
  artist: string;
  createdAt: string;
  trackId?: string | null;
}

export interface AdminDuplicateGroup {
  groupId: string;
  identity: string;
  jobs: AdminDuplicateMember[];
  tracks: AdminDuplicateMember[];
  keeperTrackId?: string | null;
}

export interface AdminDuplicateActionResult {
  itemId: string;
  kind: 'job' | 'track';
  ok: boolean;
  reason?: string | null;
}

/** Separation-only switch; unrelated to legacy NodeInfo.disabled/downloads. */
export interface AdminNode {
  id: string;
  displayName: string;
  online: boolean;
  separationEnabled: boolean;
  capabilities: Record<string, unknown>;
  agentVersion?: string | null;
  lastSeenAt?: string | null;
  busyAssignmentId?: string | null;
  currentAssignment?: {
    id: string;
    jobId: string;
    kind: string;
    status: string;
    phase?: string | null;
    heartbeatAt?: string | null;
    createdAt: string;
  } | null;
}

export interface AdminSettings {
  // Stored runtime JSON can be invalid; the API reports it with `error`.
  nodeRouting: { value: unknown; source: 'database' | 'environment'; error?: string | null };
  runpodMaxConcurrent: {
    value: unknown;
    source: 'database' | 'environment';
    error?: string | null;
  };
  runpodConfigured: boolean;
  jobsAtRunpod: number;
}

