import React, { useCallback, useEffect, useState } from 'react';
import { Activity, AlertCircle, Loader2, RefreshCw } from 'lucide-react';
import { getJobEvents, getJobs } from '@/lib/api';
import { useStore } from '@/stores/useStore';
import type { JobEvent, JobStatus } from '@/types/karaoke';
import { Button } from '@/components/ui/button';
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
} from '@/components/ui/sheet';

export const BASELINE_S_PER_MIN = 40 / 5.75;
const POLL_MS = 5000;
const STAGES: JobStatus['status'][] = [
  'pending',
  'downloading',
  'dispatched',
  'splitting',
  'verifying',
  'publishing',
];

const heartbeat = (value?: string) => {
  if (!value) return { label: 'no heartbeat', color: 'text-zinc-500' };
  const age = Math.max(0, (Date.now() - Date.parse(value)) / 1000);
  if (!Number.isFinite(age)) return { label: 'invalid heartbeat', color: 'text-red-400' };
  const label = age < 60 ? `${Math.floor(age)}s ago` : `${Math.floor(age / 60)}m ago`;
  return {
    label,
    color: age < 60 ? 'text-green-400' : age < 300 ? 'text-amber-400' : 'text-red-400',
  };
};

const title = (job: JobStatus) => job.title || job.message || job.jobId.slice(0, 8);

export const PipelineDrawer: React.FC = () => {
  const { activeDrawer, setActiveDrawer } = useStore();
  const isOpen = activeDrawer === 'pipeline';
  const [jobs, setJobs] = useState<JobStatus[]>([]);
  const [orchestratorAlive, setOrchestratorAlive] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [selectedJob, setSelectedJob] = useState<JobStatus | null>(null);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [eventsLoading, setEventsLoading] = useState(false);
  const [eventsError, setEventsError] = useState<string | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [nextJobs, healthResponse] = await Promise.all([getJobs(), fetch('/api/health')]);
      if (!healthResponse.ok) throw new Error(`Health check failed: ${healthResponse.statusText}`);
      const health: { orchestratorAlive: boolean } = await healthResponse.json();
      setJobs(nextJobs);
      setOrchestratorAlive(health.orchestratorAlive);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load pipeline');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (!isOpen) return;
    void refresh();
    const timer = window.setInterval(() => void refresh(), POLL_MS);
    return () => window.clearInterval(timer);
  }, [isOpen, refresh]);

  const showEvents = async (job: JobStatus) => {
    setSelectedJob(job);
    setEvents([]);
    setEventsError(null);
    setEventsLoading(true);
    try {
      setEvents(await getJobEvents(job.jobId));
    } catch (err) {
      setEventsError(err instanceof Error ? err.message : 'Failed to load events');
    } finally {
      setEventsLoading(false);
    }
  };

  const inFlight = jobs.filter(job => STAGES.includes(job.status));
  const terminal = jobs.filter(job => job.status === 'failed' || job.status === 'ready');

  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && setActiveDrawer('none')}>
      <SheetContent side="left" className="w-[96vw] overflow-y-auto border-r-zinc-800 bg-zinc-950 text-zinc-100 sm:max-w-[96vw] lg:max-w-6xl">
        <SheetHeader>
          <div className="flex items-center justify-between pr-8">
            <SheetTitle className="flex items-center gap-2 text-2xl text-zinc-100">
              <Activity className="h-5 w-5" /> Pipeline
            </SheetTitle>
            <Button size="icon" variant="ghost" onClick={() => void refresh()} title="Refresh pipeline">
              <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
            </Button>
          </div>
          <SheetDescription className="text-zinc-500">
            {inFlight.length > 0
              ? `${inFlight.length} job${inFlight.length === 1 ? '' : 's'} in flight`
              : `0 jobs in flight — orchestrator ${orchestratorAlive ? 'alive' : 'not responding'}`}
          </SheetDescription>
        </SheetHeader>

        {loading && jobs.length === 0 ? (
          <div className="flex items-center justify-center gap-3 p-12 text-zinc-500">
            <Loader2 className="h-6 w-6 animate-spin" /> Loading pipeline...
          </div>
        ) : error && jobs.length === 0 ? (
          <div className="flex items-center justify-center gap-2 p-12 text-red-400">
            <AlertCircle className="h-5 w-5" /> {error}
          </div>
        ) : (
          <>
            {error && <p className="mt-4 text-sm text-red-400">{error}</p>}
            <div className="mt-6 grid min-w-[1080px] grid-cols-6 gap-3 overflow-x-auto">
              {STAGES.map(stage => {
                const stageJobs = jobs.filter(job => job.status === stage);
                return (
                  <section key={stage} className="min-h-40 rounded-lg border border-zinc-800 bg-zinc-950/50 p-2">
                    <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">
                      {stage} <span className="text-zinc-700">{stageJobs.length}</span>
                    </h3>
                    <div className="space-y-2">
                      {stageJobs.map(job => {
                        const beat = heartbeat(job.workerHeartbeatAt);
                        const baseline = BASELINE_S_PER_MIN * 5.75;
                        return (
                          <button
                            key={job.jobId}
                            type="button"
                            onClick={() => void showEvents(job)}
                            className="w-full rounded-lg border border-zinc-800 bg-zinc-900 p-3 text-left transition-colors hover:border-zinc-600 hover:bg-zinc-800"
                          >
                            <p className="truncate font-medium text-zinc-200">{title(job)}</p>
                            <div className="mt-1 flex flex-wrap gap-x-2 text-xs text-zinc-500">
                              <span>attempt {job.attempt ?? 0}</span>
                              <span>{job.workerPhase || job.status}</span>
                              <span className={beat.color}>{beat.label}</span>
                            </div>
                            {job.stageTimings && Object.entries(job.stageTimings).length > 0 && (
                              <div className="mt-2 space-y-0.5 text-[11px] text-zinc-600">
                                {Object.entries(job.stageTimings).map(([timingStage, seconds]) => (
                                  <div key={timingStage}>{timingStage}: {seconds.toFixed(1)}s / {baseline.toFixed(0)}s baseline</div>
                                ))}
                              </div>
                            )}
                          </button>
                        );
                      })}
                    </div>
                  </section>
                );
              })}
            </div>

            {terminal.length > 0 && (
              <section className="mt-6">
                <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-zinc-500">Recent outcomes</h3>
                <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
                  {terminal.map(job => (
                    <button
                      type="button"
                      key={job.jobId}
                      onClick={() => void showEvents(job)}
                      className="flex items-center justify-between rounded border border-zinc-800 p-2 text-left text-sm hover:bg-zinc-900"
                    >
                      <span className="truncate text-zinc-300">{title(job)}</span>
                      <span className={job.status === 'ready' ? 'text-green-400' : 'text-red-400'}>{job.status}</span>
                    </button>
                  ))}
                </div>
              </section>
            )}

            {selectedJob && (
              <section className="mt-6 border-t border-zinc-800 pt-4">
                <h3 className="font-medium text-zinc-200">{title(selectedJob)} timeline</h3>
                {eventsLoading ? (
                  <Loader2 className="mt-4 h-5 w-5 animate-spin text-zinc-500" />
                ) : eventsError ? (
                  <p className="mt-3 text-sm text-red-400">{eventsError}</p>
                ) : (
                  <ol className="mt-3 space-y-2">
                    {events.map((event, index) => (
                      <li key={`${event.createdAt}-${index}`} className="rounded border border-zinc-800 p-2 text-sm">
                        <div className="flex justify-between gap-3">
                          <span className="font-medium text-zinc-300">{event.event}</span>
                          <time className="text-xs text-zinc-600">{new Date(event.createdAt).toLocaleString()}</time>
                        </div>
                        {event.detail && <pre className="mt-1 overflow-x-auto whitespace-pre-wrap text-xs text-zinc-500">{JSON.stringify(event.detail)}</pre>}
                      </li>
                    ))}
                    {events.length === 0 && <li className="text-sm text-zinc-500">No events recorded.</li>}
                  </ol>
                )}
              </section>
            )}
          </>
        )}
      </SheetContent>
    </Sheet>
  );
};
