import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertCircle,
  AlertTriangle,
  CheckCircle2,
  Clock,
  CloudOff,
  HelpCircle,
  Loader2,
  RefreshCw,
  ServerCog,
  ShieldAlert,
} from 'lucide-react';
import {
  AdminUnavailableError,
  applyAdminDuplicateAction,
  bulkAdminJobs,
  dismissAdminJob,
  getAdminDuplicates,
  getAdminJobDetail,
  getAdminJobs,
  getAdminNodes,
  getAdminSettings,
  getAdminTracks,
  putAdminSettings,
  releaseAdminAssignment,
  restoreAdminJob,
  retryAdminJob,
  setAdminNodeSeparationEnabled,
  setAdminTrackDeleted,
} from '@/lib/api';
import type {
  AdminBulkResult,
  AdminDuplicateActionResult,
  AdminDuplicateGroup,
  AdminJobDetail,
  AdminJobRow,
  AdminLibraryTrack,
  AdminNode,
  AdminSettings,
  EvidenceStatus,
  JobStatus,
} from '@/types/karaoke';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

const POLL_MS = 5000;
const REQUEST_TIMEOUT_MS = 10_000;
const JOB_PAGE_SIZE = 50;

const STATUS_FILTERS: (JobStatus['status'] | 'all')[] = [
  'all',
  'failed',
  'ready',
  'pending',
  'downloading',
  'dispatched',
  'splitting',
  'verifying',
  'publishing',
];

const ROUTING_MODES: { value: string; label: string; description: string }[] = [
  {
    value: 'prefer',
    label: 'Prefer nodes',
    description: 'An online node takes the work; RunPod after a 2-minute wait.',
  },
  {
    value: 'only',
    label: 'Nodes only',
    description: 'Never RunPod: jobs wait until a node is online.',
  },
  {
    value: 'off',
    label: 'Off',
    description: "Today's behavior: URL jobs are refused, RunPod separates.",
  },
  {
    value: 'download_only',
    label: 'Max performance',
    description: 'Nodes only download links; separation always goes to RunPod at once.',
  },
];

const FLAG_LABELS: Record<string, string> = {
  ready_without_track: 'ready without track row',
  open_assignment_on_terminal_job: 'open assignment on terminal job',
  node_busy_pointer_stale: 'node busy pointer names closed work',
  unresolved_dispatch_reservation: 'unresolved dispatch reservation',
  assignment_ack_overdue: 'acknowledgement overdue',
  assignment_stalled: 'assignment stalled',
  assignment_over_cap: 'assignment over age cap',
  expired_lease_on_runnable: 'expired lease on runnable work',
  wait_over_window: 'wait longer than its window',
  stage_stalled: 'no meaningful event for stage age',
};

const EVIDENCE_ICONS: Record<EvidenceStatus, typeof CheckCircle2> = {
  ok: CheckCircle2,
  missing: AlertTriangle,
  mismatch: AlertCircle,
  not_yet: Clock,
  unknown: HelpCircle,
};
const EVIDENCE_COLORS: Record<EvidenceStatus, string> = {
  ok: 'text-green-400',
  missing: 'text-amber-400',
  mismatch: 'text-red-400',
  not_yet: 'text-zinc-500',
  unknown: 'text-zinc-400',
};

const age = (value?: string | null): string => {
  if (!value) return '—';
  const parsed = Date.parse(value);
  if (!Number.isFinite(parsed)) return 'invalid time';
  const seconds = Math.max(0, (Date.now() - parsed) / 1000);
  if (seconds < 60) return `${Math.floor(seconds)}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
};

const gap = (from: string, to?: string): string => {
  if (!to) return '';
  const seconds = Math.max(0, (Date.parse(to) - Date.parse(from)) / 1000);
  if (seconds < 60) return `+${Math.floor(seconds)}s`;
  if (seconds < 3600) return `+${Math.floor(seconds / 60)}m`;
  return `+${Math.floor(seconds / 3600)}h`;
};

const jobTitle = (job: Pick<AdminJobRow, 'title' | 'jobId'>) => job.title || job.jobId.slice(0, 8);

const executorLabel = (executor?: string | null) =>
  executor?.startsWith('node:') ? executor.slice('node:'.length) : executor;

const limitDraftValue = (value: unknown) => (value == null ? '' : String(value));

const sortFailedFirst = (jobs: AdminJobRow[]): AdminJobRow[] =>
  [...jobs].sort((a, b) => {
    const aFailed = a.status === 'failed' ? 0 : 1;
    const bFailed = b.status === 'failed' ? 0 : 1;
    if (aFailed !== bFailed) return aFailed - bFailed;
    return Date.parse(b.createdAt ?? '') - Date.parse(a.createdAt ?? '');
  });

/**
 * The administrator page (/admin): failed-job detail, one-job tracing with an
 * evidence checklist, worker-node use switches, and runtime routing/RunPod
 * controls. The backing /api/admin routes are only served through the owner's
 * Tailscale-only proxy: reached from anywhere else they 404 and this page
 * says so plainly.
 */
export const AdminPage: React.FC = () => {
  const [jobs, setJobs] = useState<AdminJobRow[]>([]);
  const [jobsTotal, setJobsTotal] = useState(0);
  const [jobOffset, setJobOffset] = useState(0);
  const [counts, setCounts] = useState<Record<string, number>>({});
  const [dismissedCount, setDismissedCount] = useState(0);
  const [statusFilter, setStatusFilter] = useState<JobStatus['status'] | 'all'>('all');
  const [dismissedView, setDismissedView] = useState<'hide' | 'only' | 'include'>('hide');
  const [nodes, setNodes] = useState<AdminNode[]>([]);
  const [tracks, setTracks] = useState<AdminLibraryTrack[]>([]);
  const [trackView, setTrackView] = useState<'only' | 'include'>('include');
  const [duplicates, setDuplicates] = useState<AdminDuplicateGroup[]>([]);
  const [duplicateResults, setDuplicateResults] = useState<Record<string, AdminDuplicateActionResult[]>>({});
  const [settings, setSettings] = useState<AdminSettings | null>(null);
  const [unavailable, setUnavailable] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [busy, setBusy] = useState(false);
  const [selectedJobId, setSelectedJobId] = useState<string | null>(null);
  const [detail, setDetail] = useState<AdminJobDetail | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);
  const [verifyS3, setVerifyS3] = useState(false);
  const [routingSaving, setRoutingSaving] = useState(false);
  const [routingMessage, setRoutingMessage] = useState<string | null>(null);
  const [limitDraft, setLimitDraft] = useState<string>('');
  const [actionErrors, setActionErrors] = useState<Record<string, string>>({});
  const [resubmission, setResubmission] = useState<{ oldJobId: string; newJobId: string } | null>(null);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [bulkResults, setBulkResults] = useState<AdminBulkResult[]>([]);
  const pollAbort = useRef<AbortController | null>(null);
  const detailAbort = useRef<AbortController | null>(null);
  const detailRequest = useRef(0);
  const limitDirty = useRef(false);

  const refresh = useCallback(async () => {
    // Do not replace a slow poll at the next five-second tick: doing so
    // forever discards its jobs/nodes before the ten-second timeout can
    // settle. One controller covers all three requests and their partial results.
    if (pollAbort.current) return;
    const controller = new AbortController();
    pollAbort.current = controller;
    const timeout = window.setTimeout(() => controller.abort(), REQUEST_TIMEOUT_MS);
    setBusy(true);
    try {
      const query = statusFilter === 'all' ? undefined : statusFilter;
      const [jobsResult, nodesResult, settingsResult, tracksResult, duplicatesResult] = await Promise.allSettled([
        getAdminJobs(
          query,
          controller.signal,
          JOB_PAGE_SIZE,
          jobOffset,
          dismissedView === 'hide' ? undefined : dismissedView
        ),
        getAdminNodes(controller.signal),
        getAdminSettings(controller.signal),
        getAdminTracks(trackView, controller.signal),
        getAdminDuplicates(controller.signal),
      ]);
      if (pollAbort.current !== controller) return;
      const failures: string[] = [];
      for (const result of [jobsResult, nodesResult, settingsResult, tracksResult, duplicatesResult]) {
        if (result.status === 'rejected') {
          if (result.reason instanceof AdminUnavailableError) {
            setUnavailable(true);
            setLoaded(true);
            setBusy(false);
            window.clearTimeout(timeout);
            return;
          }
          failures.push(result.reason instanceof Error ? result.reason.message : 'request failed');
        }
      }
      setUnavailable(false);
      setError(failures.length > 0 ? failures.join('; ') : null);
      if (jobsResult.status === 'fulfilled') {
        setJobs(jobsResult.value.jobs);
        setJobsTotal(jobsResult.value.total);
        setCounts(jobsResult.value.counts);
        setDismissedCount(jobsResult.value.dismissedCount);
      }
      if (nodesResult.status === 'fulfilled') setNodes(nodesResult.value.nodes);
      if (tracksResult.status === 'fulfilled') setTracks(tracksResult.value.tracks);
      if (duplicatesResult.status === 'fulfilled') setDuplicates(duplicatesResult.value.groups);
      if (settingsResult.status === 'fulfilled') {
        setSettings(settingsResult.value);
        if (!limitDirty.current) {
          setLimitDraft(limitDraftValue(settingsResult.value.runpodMaxConcurrent.value));
        }
      }
      setLoaded(true);
    } finally {
      window.clearTimeout(timeout);
      if (pollAbort.current === controller) {
        pollAbort.current = null;
        setBusy(false);
      }
    }
  }, [statusFilter, jobOffset, dismissedView, trackView]);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => {
      window.clearInterval(timer);
      pollAbort.current?.abort();
      pollAbort.current = null;
    };
  }, [refresh]);

  const loadDetail = useCallback(
    async (jobId: string, withS3: boolean) => {
      detailAbort.current?.abort();
      const controller = new AbortController();
      detailAbort.current = controller;
      const requestId = ++detailRequest.current;
      setSelectedJobId(jobId);
      setDetailError(null);
      setDetail(null);
      try {
        const next = await getAdminJobDetail(jobId, withS3, controller.signal);
        if (detailRequest.current === requestId) setDetail(next);
      } catch (err) {
        if (detailRequest.current !== requestId || controller.signal.aborted) return;
        setDetailError(err instanceof Error ? err.message : 'Failed to load job detail');
      } finally {
        if (detailRequest.current === requestId) detailAbort.current = null;
      }
    },
    []
  );

  useEffect(() => () => {
    detailRequest.current += 1;
    detailAbort.current?.abort();
  }, []);

  useEffect(() => {
    if (selectedJobId) void loadDetail(selectedJobId, verifyS3);
  }, [selectedJobId, verifyS3, loadDetail]);

  const saveRouting = async (update: { nodeRouting?: string | null; runpodMaxConcurrent?: number | null }) => {
    setRoutingSaving(true);
    setRoutingMessage(null);
    try {
      const next = await putAdminSettings(update);
      setSettings(next);
      if ('runpodMaxConcurrent' in update) {
        limitDirty.current = false;
        setLimitDraft(limitDraftValue(next.runpodMaxConcurrent.value));
      }
      setRoutingMessage('Saved. New jobs pick it up on the next pass.');
    } catch (err) {
      setRoutingMessage(err instanceof Error ? `Save failed: ${err.message}` : 'Save failed');
    } finally {
      setRoutingSaving(false);
    }
  };

  const storedRouting = settings?.nodeRouting.value;
  const effectiveRouting = typeof storedRouting === 'string' ? storedRouting : null;
  const sortedJobs = useMemo(() => sortFailedFirst(jobs), [jobs]);
  const listedFailed = sortedJobs.filter((job) => job.status === 'failed' && !job.dismissedAt);

  const runJobAction = async (action: JobAction, jobId: string) => {
    setActionBusy(jobId);
    setActionErrors((current) => {
      const next = { ...current };
      delete next[jobId];
      return next;
    });
    try {
      const call =
        action === 'retry'
          ? retryAdminJob(jobId)
          : action === 'restore_retry'
            ? retryAdminJob(jobId, true)
            : action === 'dismiss'
              ? dismissAdminJob(jobId)
              : restoreAdminJob(jobId);
      const result = await call;
      if ('newJobId' in result && result.newJobId) {
        setResubmission({ oldJobId: jobId, newJobId: result.newJobId });
        setSelectedJobId(result.newJobId);
        await refresh();
        await loadDetail(result.newJobId, false);
      } else {
        await refresh();
        if (selectedJobId === jobId) await loadDetail(jobId, verifyS3);
      }
    } catch (err) {
      setActionErrors((current) => ({
        ...current,
        [jobId]: err instanceof Error ? err.message : 'Action failed',
      }));
    } finally {
      setActionBusy(null);
    }
  };

  const runBulk = async (action: 'retry' | 'dismiss') => {
    const count = listedFailed.length;
    if (count === 0 || !window.confirm(`${action === 'retry' ? 'Retry' : 'Dismiss'} ${count} failed jobs?`)) return;
    setBulkBusy(true);
    setBulkResults([]);
    try {
      setBulkResults(await bulkAdminJobs(action, listedFailed.map((job) => job.jobId)));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Bulk action failed');
    } finally {
      setBulkBusy(false);
    }
  };

  const runTrackAction = async (track: AdminLibraryTrack) => {
    const key = `track:${track.id}`;
    setActionBusy(key);
    setActionErrors((current) => {
      const next = { ...current };
      delete next[key];
      return next;
    });
    try {
      await setAdminTrackDeleted(track.id, !track.deletedAt);
      await refresh();
    } catch (err) {
      setActionErrors((current) => ({
        ...current,
        [key]: err instanceof Error ? err.message : 'Library action failed',
      }));
    } finally {
      setActionBusy(null);
    }
  };

  const runDuplicateAction = async (group: AdminDuplicateGroup) => {
    const action = group.keeperTrackId ? 'keep' : 'dismiss_failed';
    setActionBusy(`duplicates:${group.groupId}`);
    try {
      const results = await applyAdminDuplicateAction({
        action,
        jobIds: group.jobs.map((job) => job.id),
        trackIds: group.tracks.map((track) => track.id),
        keeperTrackId: group.keeperTrackId,
      });
      setDuplicateResults((current) => ({ ...current, [group.groupId]: results }));
      await refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Duplicate action failed');
    } finally {
      setActionBusy(null);
    }
  };

  return (
    <div className="relative h-full w-full overflow-y-auto bg-black text-white antialiased">
      <main className="mx-auto max-w-7xl space-y-8 p-6">
        <header className="space-y-1">
          <h1 className="flex items-center gap-2 text-2xl font-semibold text-zinc-100">
            <ShieldAlert className="h-5 w-5" /> Administration
          </h1>
          <p className="flex items-center gap-3 text-sm text-zinc-500">
            Job tracing, failure detail and runtime controls.
            <Button size="icon" variant="ghost" onClick={() => void refresh()} title="Refresh">
              <RefreshCw className={cn('h-4 w-4', busy && 'animate-spin')} />
            </Button>
          </p>
        </header>

        {unavailable ? (
          <div className="flex items-start gap-3 rounded-lg border border-amber-800 bg-amber-950/40 p-4 text-sm text-amber-200">
            <CloudOff className="mt-0.5 h-5 w-5 shrink-0" />
            <div>
              <p className="font-medium">The admin API is not reachable from this network.</p>
              <p className="mt-1 text-amber-300/80">
                The page shell can be public, but admin data is served only on the owner&rsquo;s
                Tailscale network; the public site answers admin API requests with 404 by design.
                Connect to Tailscale and refresh.
              </p>
            </div>
          </div>
        ) : !loaded ? (
          <div className="flex items-center justify-center gap-3 p-12 text-zinc-500">
            <Loader2 className="h-6 w-6 animate-spin" /> Loading admin state...
          </div>
        ) : (
          <>
            {error && <p className="text-sm text-red-400">{error}</p>}
            {resubmission && (
              <p className="rounded border border-green-900 bg-green-950/30 p-3 text-sm text-green-300">
                Resubmitted as a new job{' '}
                <button
                  type="button"
                  className="underline"
                  onClick={() => {
                    setVerifyS3(false);
                    setSelectedJobId(resubmission.newJobId);
                    void loadDetail(resubmission.newJobId, false);
                  }}
                >
                  New job {resubmission.newJobId.slice(0, 8)}
                </button>
              </p>
            )}

            <RoutingSection
              settings={settings}
              effectiveRouting={effectiveRouting}
              limitDraft={limitDraft}
              setLimitDraft={(value) => {
                limitDirty.current = true;
                setLimitDraft(value);
              }}
              routingSaving={routingSaving}
              routingMessage={routingMessage}
              onSave={saveRouting}
            />

            <NodesSection nodes={nodes} onChanged={() => void refresh()} />

            <section aria-label="Jobs">
              <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
                <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
                  Jobs{' '}
                  {dismissedCount > 0 && `${dismissedCount} dismissed · `}
                  {Object.entries(counts)
                    .map(([status, n]) => `${n} ${status}`)
                    .join(' · ')}
                </h2>
                <label className="flex items-center gap-2 text-xs text-zinc-500">
                  Filter
                  <select
                    value={statusFilter}
                    onChange={(event) => {
                      setJobOffset(0);
                      setStatusFilter(event.target.value as JobStatus['status'] | 'all');
                    }}
                    className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-200"
                  >
                    {STATUS_FILTERS.map((status) => (
                      <option key={status} value={status}>
                        {status}
                      </option>
                    ))}
                  </select>
                </label>
                <label className="flex items-center gap-2 text-xs text-zinc-500">
                  View
                  <select
                    value={dismissedView}
                    onChange={(event) => {
                      setJobOffset(0);
                      setDismissedView(event.target.value as 'hide' | 'only' | 'include');
                    }}
                    className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-200"
                    aria-label="Dismissed jobs"
                  >
                    <option value="hide">active</option>
                    <option value="only">dismissed</option>
                    <option value="include">include dismissed</option>
                  </select>
                </label>
              </div>
              {listedFailed.length > 0 && (
                <div className="mb-3 flex flex-wrap gap-2">
                  <Button size="sm" variant="outline" disabled={bulkBusy} onClick={() => void runBulk('retry')}>
                    Retry all failed ({listedFailed.length})
                  </Button>
                  <Button size="sm" variant="outline" disabled={bulkBusy} onClick={() => void runBulk('dismiss')}>
                    Dismiss all failed ({listedFailed.length})
                  </Button>
                </div>
              )}
              {bulkResults.length > 0 && (
                <ul aria-label="Bulk action results" className="mb-3 space-y-1 text-xs">
                  {bulkResults.map((result, index) => (
                    <li key={`${result.jobId}-${index}`} className={result.ok ? 'text-green-400' : 'text-red-400'}>
                      {result.jobId.slice(0, 8)}: {result.ok ? `ok${result.action ? ` (${result.action})` : result.resumeStage ? ` (${result.resumeStage})` : ''}${result.newJobId ? ` → ${result.newJobId.slice(0, 8)}` : ''}` : result.reason}
                    </li>
                  ))}
                </ul>
              )}
              {sortedJobs.length === 0 ? (
                <p className="text-sm text-zinc-600">No jobs match this filter.</p>
              ) : (
                <div className="grid gap-2 lg:grid-cols-2">
                  {sortedJobs.map((job) => (
                    <JobCard
                      key={job.jobId}
                      job={job}
                      error={actionErrors[job.jobId]}
                      busy={actionBusy === job.jobId}
                      onSelect={() => {
                        setVerifyS3(false);
                        void loadDetail(job.jobId, false);
                      }}
                      onAction={runJobAction}
                    />
                  ))}
                </div>
              )}
              <div className="mt-3 flex flex-wrap items-center justify-between gap-2 text-xs text-zinc-500">
                <span>
                  {jobsTotal === 0
                    ? '0 jobs'
                    : `${jobOffset + 1}–${Math.min(jobOffset + jobs.length, jobsTotal)} of ${jobsTotal}`}
                </span>
                <div className="flex gap-2">
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={jobOffset === 0}
                    onClick={() => setJobOffset((offset) => Math.max(0, offset - JOB_PAGE_SIZE))}
                  >
                    Previous page
                  </Button>
                  <Button
                    size="sm"
                    variant="outline"
                    disabled={jobOffset + JOB_PAGE_SIZE >= jobsTotal}
                    onClick={() => setJobOffset((offset) => offset + JOB_PAGE_SIZE)}
                  >
                    Next page
                  </Button>
                </div>
              </div>
            </section>

            {selectedJobId && (
              <JobDetailSection
                jobId={selectedJobId}
                detail={detail}
                error={detailError}
                verifyS3={verifyS3}
                onToggleVerify={() => setVerifyS3((value) => !value)}
                onReleased={() => selectedJobId && void loadDetail(selectedJobId, verifyS3)}
                actionError={actionErrors[selectedJobId]}
                actionBusy={actionBusy === selectedJobId}
                onAction={runJobAction}
              />
            )}
            <LibrarySection
              tracks={tracks}
              view={trackView}
              onView={setTrackView}
              actionBusy={actionBusy}
              errors={actionErrors}
              onAction={runTrackAction}
            />
            <DuplicatesSection
              groups={duplicates}
              results={duplicateResults}
              actionBusy={actionBusy}
              onAction={runDuplicateAction}
            />
          </>
        )}
      </main>
    </div>
  );
};

// --- job and library actions -----------------------------------------------------

type JobAction = 'retry' | 'dismiss' | 'restore' | 'restore_retry';

type ActionJob = Pick<AdminJobRow, 'jobId' | 'status' | 'dismissedAt'>;

const JobActionButtons: React.FC<{
  job: ActionJob;
  busy: boolean;
  onAction: (action: JobAction, jobId: string) => void;
}> = ({ job, busy, onAction }) => {
  if (job.dismissedAt) {
    // G7/G8: a dismissed job cannot be retried until restored, so the
    // dismissed view offers exactly Restore and Restore and retry — never a
    // plain Retry.
    return (
      <span className="flex gap-2">
        <Button size="sm" variant="outline" disabled={busy} onClick={() => onAction('restore', job.jobId)}>Restore</Button>
        <Button size="sm" variant="outline" disabled={busy} onClick={() => onAction('restore_retry', job.jobId)}>Restore and retry</Button>
      </span>
    );
  }
  if (job.status !== 'failed') return null;
  return (
    <span className="flex gap-2">
      <Button size="sm" variant="outline" disabled={busy} onClick={() => onAction('retry', job.jobId)}>Retry</Button>
      <Button size="sm" variant="outline" disabled={busy} onClick={() => onAction('dismiss', job.jobId)}>Dismiss</Button>
    </span>
  );
};

const JobCard: React.FC<{
  job: AdminJobRow;
  error?: string;
  busy: boolean;
  onSelect: () => void;
  onAction: (action: JobAction, jobId: string) => void;
}> = ({ job, error, busy, onSelect, onAction }) => (
  <div className={cn(
    'w-full rounded-lg border p-3 text-left transition-colors',
    job.status === 'failed' ? 'border-red-900/70 bg-zinc-950' : 'border-zinc-800 bg-zinc-950/50'
  )}>
    <button type="button" onClick={onSelect} className="block w-full text-left hover:bg-zinc-800">
      <span className="flex items-center justify-between gap-2">
        <span className="truncate font-medium text-zinc-200">{jobTitle(job)}</span>
        <span className={cn(
          'shrink-0 text-xs font-medium',
          job.status === 'failed' ? 'text-red-400' : job.status === 'ready' ? 'text-green-400' : 'text-zinc-400'
        )}>
          {job.status}{job.dismissedAt ? ' · dismissed' : ''}
        </span>
      </span>
      <span className="mt-1 flex flex-wrap gap-x-3 text-xs text-zinc-500">
        <span>attempt {job.attempt}</span>
        {executorLabel(job.executor) && <span>{executorLabel(job.executor)}</span>}
        <span>{job.workerPhase || job.stage}</span>
        <span>updated {age(job.updatedAt)}</span>
      </span>
      {job.errorCode && (
        <span className="mt-2 block text-xs">
          <span className="rounded bg-red-950/70 px-1.5 py-0.5 font-mono text-red-300">{job.errorCode}</span>
          {job.errorDetail && <span className="ml-2 text-zinc-400">{job.errorDetail}</span>}
        </span>
      )}
      {job.flags.length > 0 && (
        <span className="mt-2 flex flex-wrap gap-1">
          {job.flags.map((flag) => (
            <span key={flag} title={flag} className="rounded-full border border-amber-800/70 bg-amber-950/40 px-2 py-0.5 text-[11px] text-amber-300">
              {FLAG_LABELS[flag] ?? flag}
            </span>
          ))}
        </span>
      )}
    </button>
    <span className="mt-2 flex items-center justify-between gap-2">
      <JobActionButtons job={job} busy={busy} onAction={onAction} />
      {error && <span className="text-xs text-red-400">{error}</span>}
    </span>
  </div>
);

const LibrarySection: React.FC<{
  tracks: AdminLibraryTrack[];
  view: 'only' | 'include';
  onView: (view: 'only' | 'include') => void;
  actionBusy: string | null;
  errors: Record<string, string>;
  onAction: (track: AdminLibraryTrack) => void;
}> = ({ tracks, view, onView, actionBusy, errors, onAction }) => (
  <section aria-label="Library" className="border-t border-zinc-800 pt-4">
    <div className="mb-3 flex items-center justify-between gap-2">
      <h2 className="text-xs font-semibold uppercase tracking-wide text-zinc-500">Library</h2>
      <label className="flex items-center gap-2 text-xs text-zinc-500">
                    Deleted tracks
        <select
          value={view}
          onChange={(event) => onView(event.target.value as 'only' | 'include')}
          className="rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-zinc-200"
        >
          <option value="include">include</option>
          <option value="only">only</option>
        </select>
      </label>
    </div>
    {tracks.length === 0 ? <p className="text-sm text-zinc-600">No tracks match this view.</p> : (
      <div className="grid gap-2 lg:grid-cols-2">
        {tracks.map((track) => (
          <div key={track.id} className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3 text-sm">
            <p className="font-medium text-zinc-200">{track.title}</p>
            <p className="mt-1 text-xs text-zinc-500">
              {track.artist} · {track.durationSeconds}s · generation {track.generation}
              {track.deletedAt ? ' · removed' : ''}
            </p>
            <div className="mt-2 flex items-center justify-between gap-2">
              <Button size="sm" variant="outline" disabled={actionBusy === `track:${track.id}`} onClick={() => onAction(track)}>
                {track.deletedAt ? 'Restore' : 'Remove from library'}
              </Button>
              {errors[`track:${track.id}`] && <span className="text-xs text-red-400">{errors[`track:${track.id}`]}</span>}
            </div>
          </div>
        ))}
      </div>
    )}
  </section>
);

const duplicateHeading = (group: AdminDuplicateGroup): string => {
  const member = [...group.tracks, ...group.jobs][0];
  const title = member?.title?.trim() || 'Untitled song';
  const artist = member?.artist?.trim();
  return artist ? `${title} — ${artist}` : title;
};

const duplicateReason = (group: AdminDuplicateGroup): string => {
  if (group.identity.startsWith('checksum:')) return 'Same file';
  if (group.identity.startsWith('youtube:')) return 'Same YouTube video';
  return 'Same title and artist';
};

const DuplicatesSection: React.FC<{
  groups: AdminDuplicateGroup[];
  results: Record<string, AdminDuplicateActionResult[]>;
  actionBusy: string | null;
  onAction: (group: AdminDuplicateGroup) => void;
}> = ({ groups, results, actionBusy, onAction }) => (
  <section aria-label="Duplicates" className="border-t border-zinc-800 pt-4">
    <h2 className="mb-3 text-xs font-semibold uppercase tracking-wide text-zinc-500">Duplicates</h2>
    {groups.length === 0 ? <p className="text-sm text-zinc-600">No duplicate jobs or tracks.</p> : (
      <div className="space-y-3">
        {groups.map((group) => (
          <div key={group.groupId} className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3 text-sm">
            <p className="font-medium text-zinc-200">{duplicateHeading(group)}</p>
            <p className="mt-1 text-xs text-zinc-500">{duplicateReason(group)}</p>
            <p className="mt-1 text-zinc-300">
              {group.jobs.length} jobs · {group.tracks.length} tracks
              {group.keeperTrackId ? ` · keeper ${group.keeperTrackId.slice(0, 8)}` : ' · no ready keeper'}
            </p>
            <p className="mt-1 text-xs text-zinc-500">
              {[...group.jobs, ...group.tracks].map((member) => `${member.title ?? member.id.slice(0, 8)} (${member.status})`).join(' · ')}
            </p>
            <Button
              size="sm"
              variant="outline"
              className="mt-2"
              disabled={actionBusy === `duplicates:${group.groupId}`}
              onClick={() => onAction(group)}
            >
              {group.keeperTrackId ? 'Keep the good one, clear the rest' : 'Dismiss failed'}
            </Button>
            {results[group.groupId] && (
              <ul className="mt-2 space-y-1 text-xs">
                {results[group.groupId].map((result, index) => (
                  <li key={`${result.itemId}-${index}`} className={result.ok ? 'text-green-400' : 'text-red-400'}>
                    {result.kind} {result.itemId.slice(0, 8)}: {result.ok ? 'ok' : result.reason}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    )}
  </section>
);

// --- routing + RunPod limit -----------------------------------------------------

const RoutingSection: React.FC<{
  settings: AdminSettings | null;
  effectiveRouting: string | null;
  limitDraft: string;
  setLimitDraft: (value: string) => void;
  routingSaving: boolean;
  routingMessage: string | null;
  onSave: (update: { nodeRouting?: string | null; runpodMaxConcurrent?: number | null }) => void;
}> = ({ settings, effectiveRouting, limitDraft, setLimitDraft, routingSaving, routingMessage, onSave }) => {
  if (!settings) return null;
  const sourceLabel = (source: 'database' | 'environment') =>
    source === 'database' ? 'set here' : 'from environment';
  return (
    <section aria-label="Routing" className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-4">
      <h2 className="mb-3 flex items-center gap-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
        <ServerCog className="h-4 w-4" /> Routing
      </h2>
      <div className="grid gap-2 sm:grid-cols-2">
        {ROUTING_MODES.map((mode) => {
          const active = effectiveRouting === mode.value;
          return (
            <label
              key={mode.value}
              className={cn(
                'flex cursor-pointer items-start gap-3 rounded-lg border p-3 text-sm transition-colors',
                active
                  ? 'border-zinc-500 bg-zinc-900'
                  : 'border-zinc-800 bg-zinc-950 hover:border-zinc-600'
              )}
            >
              <input
                type="radio"
                name="node-routing"
                checked={active}
                onChange={() => onSave({ nodeRouting: mode.value })}
                className="mt-1"
                aria-label={mode.label}
              />
              <span>
                <span className="block font-medium text-zinc-200">{mode.label}</span>
                <span className="block text-xs text-zinc-500">{mode.description}</span>
              </span>
            </label>
          );
        })}
      </div>
      <p className="mt-2 text-xs text-zinc-500">
        Current mode:{' '}
        <span className="text-zinc-300">{effectiveRouting ?? 'unknown'}</span> (
        {sourceLabel(settings.nodeRouting.source)})
        {settings.nodeRouting.error && (
          <span className="text-amber-400"> — stored value invalid: {settings.nodeRouting.error}</span>
        )}
      </p>

      <div className="mt-5 flex flex-wrap items-end gap-3 border-t border-zinc-800 pt-4">
        <div>
          <p className="text-xs font-semibold uppercase tracking-wide text-zinc-500">
            RunPod jobs at once
          </p>
          <p className="mt-1 text-xs text-zinc-500">
            {settings.jobsAtRunpod} at RunPod now ·{' '}
            {settings.runpodConfigured ? 'RunPod configured' : 'RunPod not configured'} · limit{' '}
            {sourceLabel(settings.runpodMaxConcurrent.source)}
          </p>
          <div className="mt-2 flex items-center gap-2">
            <input
              type="number"
              min={0}
              value={limitDraft}
              onChange={(event) => setLimitDraft(event.target.value)}
              placeholder="no limit"
              className="w-24 rounded border border-zinc-700 bg-zinc-900 px-2 py-1 text-sm text-zinc-200"
              aria-label="RunPod concurrency limit"
            />
            <Button
              size="sm"
              variant="outline"
              disabled={routingSaving || limitDraft === ''}
              onClick={() => {
                const parsed = Number.parseInt(limitDraft, 10);
                if (Number.isFinite(parsed) && parsed >= 0) onSave({ runpodMaxConcurrent: parsed });
              }}
            >
              Set limit
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={routingSaving}
              onClick={() => onSave({ runpodMaxConcurrent: null })}
            >
              No limit
            </Button>
            <Button
              size="sm"
              variant="outline"
              disabled={routingSaving}
              onClick={() => {
                setLimitDraft('0');
                onSave({ runpodMaxConcurrent: 0 });
              }}
            >
              Paused
            </Button>
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            No limit stores an unlimited database override; it does not restore the environment default.
          </p>
        </div>
        {routingSaving && <Loader2 className="h-4 w-4 animate-spin text-zinc-500" />}
        {routingMessage && <p className="text-xs text-zinc-400">{routingMessage}</p>}
      </div>
    </section>
  );
};

// --- nodes ------------------------------------------------------------------------

const NodesSection: React.FC<{ nodes: AdminNode[]; onChanged: () => void }> = ({ nodes, onChanged }) => {
  // Errors stay associated with the node that produced them: toggling another
  // node must neither clear nor overwrite this node's failure message.
  const [errors, setErrors] = useState<Record<string, string>>({});
  const [pendingNode, setPendingNode] = useState<string | null>(null);

  const toggle = async (node: AdminNode, enabled: boolean) => {
    setPendingNode(node.id);
    setErrors((prev) => {
      const next = { ...prev };
      delete next[node.id];
      return next;
    });
    try {
      await setAdminNodeSeparationEnabled(node.id, enabled);
      onChanged();
    } catch (err) {
      // No optimistic write: the checkbox remains at the confirmed state.
      const message = err instanceof Error ? err.message : `Failed to update ${node.displayName}`;
      setErrors((prev) => ({ ...prev, [node.id]: message }));
    } finally {
      setPendingNode(null);
    }
  };

  return (
    <section aria-label="Worker nodes">
      <h2 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Nodes</h2>
      {nodes.length === 0 ? (
        <p className="text-sm text-zinc-600">No worker nodes registered.</p>
      ) : (
        <div className="grid gap-2 lg:grid-cols-2">
          {nodes.map((node) => {
            const device = node.capabilities?.device;
            return (
              <div
                key={node.id}
                className="rounded-lg border border-zinc-800 bg-zinc-950/50 p-3"
                title={`${node.id}${node.agentVersion ? ` · agent ${node.agentVersion}` : ''}`}
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="flex items-center gap-2 font-medium text-zinc-200">
                    <span
                      role="img"
                      aria-label={node.online ? 'online' : 'offline'}
                      className={cn('h-2 w-2 rounded-full', node.online ? 'bg-green-400' : 'bg-zinc-600')}
                    />
                    {node.displayName}
                    {typeof device === 'string' && device && (
                      <span className="text-xs text-zinc-500">{device}</span>
                    )}
                  </span>
                  <label className="flex items-center gap-2 text-xs text-zinc-400">
                    Separate stems on this node
                    <input
                      type="checkbox"
                      checked={node.separationEnabled}
                      disabled={pendingNode === node.id}
                      onChange={(event) => void toggle(node, event.target.checked)}
                      aria-label={`Separate stems on this node: ${node.displayName}`}
                    />
                  </label>
                </div>
                <div className="mt-1 text-xs text-zinc-500">
                  seen {age(node.lastSeenAt)} ·{' '}
                  {node.currentAssignment
                    ? `${node.currentAssignment.kind} ${node.currentAssignment.status} (${node.currentAssignment.phase ?? '—'}), heartbeat ${age(node.currentAssignment.heartbeatAt)}`
                    : 'idle'}
                  {!node.separationEnabled && <span className="text-red-400"> · separation unchecked</span>}
                </div>
                {errors[node.id] && (
                  <p role="alert" data-node-error={node.id} className="mt-2 text-sm text-red-400">
                    {errors[node.id]}
                  </p>
                )}
              </div>
            );
          })}
        </div>
      )}
      <p className="mt-2 text-xs text-zinc-500">
        Unchecked nodes still download; they just hand separation to another node or RunPod.
      </p>
    </section>
  );
};

// --- job detail: timeline, assignments, evidence -----------------------------------

const OPEN_ASSIGNMENT_STATUSES = new Set(['assigned', 'acknowledged', 'running']);
const TERMINAL_JOB_STATUSES = new Set(['ready', 'failed']);

const JobDetailSection: React.FC<{
  jobId: string;
  detail: AdminJobDetail | null;
  error: string | null;
  verifyS3: boolean;
  onToggleVerify: () => void;
  onReleased: () => void;
  actionError?: string;
  actionBusy: boolean;
  onAction: (action: JobAction, jobId: string) => void;
}> = ({ jobId, detail, error, verifyS3, onToggleVerify, onReleased, actionError, actionBusy, onAction }) => {
  const [releaseBusy, setReleaseBusy] = useState<string | null>(null);
  const [releaseError, setReleaseError] = useState<string | null>(null);
  const terminal = detail ? TERMINAL_JOB_STATUSES.has(detail.job.status) : false;
  return (
  <section aria-label="Job detail" className="border-t border-zinc-800 pt-4">
    <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
      <h2 className="font-medium text-zinc-200">
        Job {jobId.slice(0, 8)} trace
        {detail && (
          <span className="ml-2 text-xs text-zinc-500">
            track {detail.derivedTrackId.slice(0, 8)} · attempt key{' '}
            {detail.dispatchAttempt.idempotencyKey?.slice(-4) || 'unknown / legacy'}
          </span>
        )}
      </h2>
      <span className="flex flex-wrap gap-2">
        {detail && <JobActionButtons job={detail.job} busy={actionBusy} onAction={onAction} />}
        <Button size="sm" variant={verifyS3 ? 'default' : 'outline'} onClick={onToggleVerify}>
          {verifyS3 ? 'Checking storage…' : 'Check storage'}
        </Button>
      </span>
    </div>
    {actionError && <p className="mb-2 text-sm text-red-400">{actionError}</p>}
    {error ? (
      <p className="text-sm text-red-400">{error}</p>
    ) : !detail ? (
      <Loader2 className="h-5 w-5 animate-spin text-zinc-500" />
    ) : (
      <div className="grid gap-6 lg:grid-cols-2">
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
            Timeline
          </h3>
          {detail.events.length === 0 ? (
            <p className="text-sm text-zinc-600">No job events recorded.</p>
          ) : (
            <ol className="space-y-1.5">
              {detail.events.map((event, index) => {
                const previous = detail.events[index - 1];
                return (
                  <li key={event.id} className="rounded border border-zinc-800 p-2 text-sm">
                    <div className="flex items-baseline justify-between gap-3">
                      <span className="font-medium text-zinc-300">{event.event}</span>
                      <span className="text-xs text-zinc-600">
                        {previous && <span className="mr-2 text-zinc-700">{gap(previous.createdAt, event.createdAt)}</span>}
                        {new Date(event.createdAt).toLocaleTimeString()}
                      </span>
                    </div>
                    {event.detail && Object.keys(event.detail).length > 0 && (
                      <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-xs text-zinc-500">
                        {JSON.stringify(event.detail)}
                      </pre>
                    )}
                  </li>
                );
              })}
            </ol>
          )}
          <h3 className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-zinc-500">
            Assignments
          </h3>
          {detail.assignments.length === 0 ? (
            <p className="text-sm text-zinc-600">No node assignments.</p>
          ) : (
            <ul className="space-y-1.5">
              {detail.assignments.map((assignment) => (
                <li key={assignment.id} className="rounded border border-zinc-800 p-2 text-sm">
                  <div className="flex flex-wrap gap-x-3 text-xs text-zinc-500">
                    <span className="font-medium text-zinc-300">{assignment.nodeId}</span>
                    <span>{assignment.kind}</span>
                    <span
                      className={cn(
                        assignment.status === 'failed' || assignment.status === 'expired'
                          ? 'text-red-400'
                          : assignment.status === 'completed'
                            ? 'text-green-400'
                            : 'text-zinc-400'
                      )}
                    >
                      {assignment.status}
                    </span>
                    {assignment.phase && <span>{assignment.phase}</span>}
                    <span>heartbeat {age(assignment.heartbeatAt)}</span>
                    {terminal && OPEN_ASSIGNMENT_STATUSES.has(assignment.status) && (
                      <Button
                        size="sm"
                        variant="outline"
                        disabled={releaseBusy === assignment.id}
                        onClick={() => {
                          setReleaseBusy(assignment.id);
                          setReleaseError(null);
                          void releaseAdminAssignment(assignment.id)
                            .then(onReleased)
                            .catch((err: unknown) =>
                              setReleaseError(err instanceof Error ? err.message : 'Release failed')
                            )
                            .finally(() => setReleaseBusy(null));
                        }}
                      >
                        {releaseBusy === assignment.id ? 'Releasing…' : 'Release node'}
                      </Button>
                    )}
                  </div>
                  {assignment.detail && (
                    <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-xs text-zinc-500">
                      {JSON.stringify(assignment.detail)}
                    </pre>
                  )}
                </li>
              ))}
            </ul>
          )}
          <h3 className="mb-2 mt-4 text-xs font-semibold uppercase tracking-wide text-zinc-500">
            Track
          </h3>
          {detail.track ? (
            <p className="text-sm text-zinc-400">
              {detail.track.title} · generation {detail.track.generation} · {detail.track.id}
            </p>
          ) : (
            <p className="text-sm text-zinc-600">No track row.</p>
          )}
          {releaseError && <p className="mt-2 text-xs text-red-400">{releaseError}</p>}
        </div>
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
            Evidence {verifyS3 && '(with storage checks)'}
          </h3>
          {detail.evidence.length === 0 ? (
            <p className="text-sm text-zinc-600">No evidence checks recorded.</p>
          ) : (
            <ul className="space-y-1">
              {detail.evidence.map((item) => {
                const Icon = EVIDENCE_ICONS[item.status];
                return (
                  <li key={`${item.stage}-${item.name}`} className="rounded border border-zinc-800 p-2 text-sm">
                    <div className="flex items-center justify-between gap-2">
                      <span className="flex items-center gap-2">
                        <Icon className={cn('h-4 w-4 shrink-0', EVIDENCE_COLORS[item.status])} />
                        <span className="font-medium text-zinc-300">{item.name}</span>
                      </span>
                      <span className="text-[11px] uppercase tracking-wide text-zinc-600">
                        {item.stage} · {item.status}
                      </span>
                    </div>
                    <p className="mt-0.5 text-xs text-zinc-500">expected: {item.expected}</p>
                    <p className="text-xs text-zinc-400">observed: {item.observed}</p>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      </div>
    )}
  </section>
  );
};
