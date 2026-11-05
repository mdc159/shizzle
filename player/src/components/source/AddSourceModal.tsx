import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useStore } from '@/stores/useStore';
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter
} from '@/components/ui/dialog';
import { Button } from '@/components/ui/button';
import { Loader2, Check, X, AlertCircle, Upload, Download, Link as LinkIcon } from 'lucide-react';
import { uploadFile, submitUrl, getJobStatus } from '@/lib/api';
import { parseSourceTitle } from '@/lib/sourceTitle';
import { toast } from 'sonner';
import type { JobStatus } from '@/types/karaoke';
import { cn } from '@/lib/utils';

// Processing steps for progress display (Phase 2 job stages). Labels for
// `downloading` and `dispatched` depend on who holds the job: a worker node
// (docs/WORKER-NODES.md) or the cloud. See stepLabel().
const STEPS = [
  { status: 'pending', label: 'Queued' },
  { status: 'downloading', label: 'Fetching source' },
  { status: 'dispatched', label: 'Separating (cloud)' },
  { status: 'splitting', label: 'Separating Stems' },
  { status: 'verifying', label: 'Verifying' },
  { status: 'publishing', label: 'Publishing' },
  { status: 'ready', label: 'Complete' },
] as const;

// Polling configuration
const BASE_POLL_INTERVAL = 3000;
const MAX_POLL_INTERVAL = 30000;
const MAX_POLL_DURATION = 60 * 60 * 1000; // 1 hour (Demucs can be slow)
// A URL job waits for a worker node to come online (up to the server's
// node_download_wait_seconds, 24 h by default), so its poll window is longer.
const MAX_URL_POLL_DURATION = 24 * 60 * 60 * 1000;

/** The node id from an executor string ("node:mac-mini" -> "mac-mini"), else null. */
const executorNode = (executor?: string | null): string | null =>
  executor && executor.startsWith('node:') ? executor.slice('node:'.length) : null;

/** Stage label, node-aware for the separation step. */
const stepLabel = (status: string, label: string, job: JobStatus | null): string => {
  if (status !== 'dispatched') return label;
  const node = executorNode(job?.executor);
  return node ? `Separating on ${node}` : label;
};

/** Human copy for a structured failure; falls back to the server's detail. */
const failureMessage = (status: JobStatus): string => {
  if (status.errorCode === 'NO_NODE_AVAILABLE') {
    return 'No download node was online within the wait window. Start the node agent and try again.';
  }
  return status.error || 'Processing failed';
};

/** Lenient check that the pasted text looks like an http(s) link. */
const looksLikeUrl = (value: string): boolean => /^https?:\/\/\S+$/i.test(value.trim());

export const AddSourceModal: React.FC = () => {
  const { activeDrawer, setActiveDrawer, setActiveJob } = useStore();
  const isOpen = activeDrawer === 'source';

  const [sourceUrl, setSourceUrl] = useState('');
  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [title, setTitle] = useState('');
  const [artist, setArtist] = useState('');
  const [artistUncertain, setArtistUncertain] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const pollIntervalRef = useRef<number | null>(null);
  const pollStartTimeRef = useRef<number>(0);
  const pollDurationRef = useRef<number>(MAX_POLL_DURATION);
  const consecutiveErrorsRef = useRef<number>(0);
  const currentPollIntervalRef = useRef<number>(BASE_POLL_INTERVAL);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const urlMode = looksLikeUrl(sourceUrl);

  const stopPolling = () => {
    if (pollIntervalRef.current) {
      clearTimeout(pollIntervalRef.current);
      pollIntervalRef.current = null;
    }
    consecutiveErrorsRef.current = 0;
    currentPollIntervalRef.current = BASE_POLL_INTERVAL;
  };

  useEffect(() => {
    return () => stopPolling();
  }, []);

  useEffect(() => {
    if (!isOpen) stopPolling();
  }, [isOpen]);

  const schedulePoll = (id: string) => {
    pollIntervalRef.current = window.setTimeout(async () => {
      const elapsed = Date.now() - pollStartTimeRef.current;
      if (elapsed > pollDurationRef.current) {
        stopPolling();
        setError('Processing timeout — job may be stuck.');
        setLoading(false);
        toast.error('Processing timeout');
        return;
      }

      try {
        const status = await getJobStatus(id);
        setJobStatus(status);
        setActiveJob(status);

        consecutiveErrorsRef.current = 0;
        currentPollIntervalRef.current = BASE_POLL_INTERVAL;

        if (status.status === 'ready') {
          stopPolling();
          toast.success('Track ready!');
        } else if (status.status === 'failed') {
          stopPolling();
          setError(failureMessage(status));
          setLoading(false);
          toast.error('Processing failed');
        } else {
          schedulePoll(id);
        }
      } catch {
        consecutiveErrorsRef.current++;
        currentPollIntervalRef.current = Math.min(
          BASE_POLL_INTERVAL * Math.pow(2, consecutiveErrorsRef.current),
          MAX_POLL_INTERVAL
        );
        schedulePoll(id);
      }
    }, currentPollIntervalRef.current);
  };

  const startPolling = (id: string, maxDuration: number) => {
    stopPolling();
    pollStartTimeRef.current = Date.now();
    pollDurationRef.current = maxDuration;
    schedulePoll(id);
  };

  const handleFileSelect = useCallback((file: File) => {
    setSelectedFile(file);
    setError(null);
    // Prefill clean metadata from the file name; both stay editable.
    const parsed = parseSourceTitle(file.name);
    setTitle(parsed.title);
    setArtist(parsed.artist);
    setArtistUncertain(!parsed.confident);
  }, []);

  const handleDrop = useCallback((e: React.DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file && file.type.startsWith('video/')) {
      handleFileSelect(file);
    } else {
      setError('Please drop a video file (MP4, MKV, etc.)');
    }
  }, [handleFileSelect]);

  const handleSubmit = async () => {
    if (urlMode) {
      setLoading(true);
      setError(null);
      setJobStatus(null);
      try {
        const result = await submitUrl(sourceUrl, {
          title: title.trim() || undefined,
          artist: artist.trim() || undefined,
        });
        setJobId(result.jobId);
        setJobStatus({ jobId: result.jobId, status: 'pending', sourceType: 'url' });
        toast.success('Link submitted');
        startPolling(result.jobId, MAX_URL_POLL_DURATION);
      } catch (err) {
        setError(err instanceof Error ? err.message : 'Failed to submit link');
        setLoading(false);
        toast.error('Failed to submit link');
      }
      return;
    }

    if (!selectedFile || !title.trim() || !artist.trim()) return;

    setLoading(true);
    setError(null);
    setJobStatus(null);

    try {
      const result = await uploadFile(selectedFile, {
        title: title.trim(),
        artist: artist.trim(),
      });
      setJobId(result.jobId);
      setJobStatus({ jobId: result.jobId, status: 'pending' });
      toast.success('Processing started');
      startPolling(result.jobId, MAX_POLL_DURATION);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to upload');
      setLoading(false);
      toast.error('Failed to upload file');
    }
  };

  const handleClose = () => {
    stopPolling();
    setSourceUrl('');
    setSelectedFile(null);
    setTitle('');
    setArtist('');
    setArtistUncertain(false);
    setLoading(false);
    setError(null);
    setJobId(null);
    setJobStatus(null);
    setActiveJob(null);
    setActiveDrawer('none');
  };

  const handleRetry = () => {
    setError(null);
    setJobStatus(null);
    setJobId(null);
    setLoading(false);
  };

  const getCurrentStepIndex = () => {
    if (!jobStatus) return -1;
    return STEPS.findIndex(s => s.status === jobStatus.status);
  };

  const currentStepIndex = getCurrentStepIndex();

  const canSubmit = urlMode
    ? !loading
    : !!selectedFile && !!title.trim() && !!artist.trim() && !loading;

  const showMetadata = urlMode || !!selectedFile;

  const downloadUrl = jobId ? `/api/tracks/${jobId}/download` : null;

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && handleClose()}>
      <DialogContent className="sm:max-w-[425px] bg-zinc-950 border-zinc-800 text-zinc-100">
        <DialogHeader>
          <DialogTitle>Add Source</DialogTitle>
          <DialogDescription className="text-zinc-500">
            {jobStatus ? 'Processing your track...' : 'Paste a YouTube link or drop a video file to split stems.'}
          </DialogDescription>
        </DialogHeader>

        {!jobStatus ? (
          // Source input: URL field above the file drop zone
          <div className="space-y-4 py-4">
            <div className="space-y-1.5">
              <label htmlFor="source-url" className="text-xs font-medium text-zinc-400">
                YouTube link
              </label>
              <div className="relative">
                <LinkIcon className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-zinc-500" />
                <input
                  id="source-url"
                  type="url"
                  inputMode="url"
                  autoComplete="off"
                  value={sourceUrl}
                  onChange={(e) => { setSourceUrl(e.target.value); setError(null); }}
                  placeholder="Paste a YouTube link"
                  className="w-full rounded-md border border-zinc-700 bg-zinc-900 py-2 pl-9 pr-3 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-500"
                />
              </div>
              {urlMode && (
                <p className="text-xs text-zinc-500">
                  A worker node downloads and separates the link. The file drop below is ignored.
                </p>
              )}
              {sourceUrl.trim() && !urlMode && (
                <p className="text-xs text-amber-400">Enter a full http(s) link.</p>
              )}
            </div>

            <div
              className={cn(
                "border-2 border-dashed rounded-lg p-8 text-center transition-colors",
                urlMode
                  ? "border-zinc-800 opacity-40 cursor-not-allowed"
                  : dragOver
                    ? "border-white bg-white/5 cursor-pointer"
                    : selectedFile
                      ? "border-green-500/50 bg-green-500/5 cursor-pointer"
                      : "border-zinc-700 hover:border-zinc-500 cursor-pointer"
              )}
              aria-disabled={urlMode}
              onDragOver={(e) => { e.preventDefault(); if (!urlMode) setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={(e) => { if (urlMode) { e.preventDefault(); return; } handleDrop(e); }}
              onClick={() => { if (!urlMode) fileInputRef.current?.click(); }}
            >
              <input
                ref={fileInputRef}
                type="file"
                accept="video/*"
                className="hidden"
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  if (file) handleFileSelect(file);
                }}
              />

              {selectedFile ? (
                <div className="space-y-2">
                  <Check className="h-8 w-8 text-green-400 mx-auto" />
                  <p className="text-sm text-zinc-300 font-medium truncate">{selectedFile.name}</p>
                  <p className="text-xs text-zinc-500">
                    {(selectedFile.size / (1024 * 1024)).toFixed(1)} MB — click to change
                  </p>
                </div>
              ) : (
                <div className="space-y-2">
                  <Upload className="h-8 w-8 text-zinc-500 mx-auto" />
                  <p className="text-sm text-zinc-400">
                    Drag & drop a video file here
                  </p>
                  <p className="text-xs text-zinc-600">or click to browse</p>
                </div>
              )}
            </div>

            {showMetadata && (
              <div className="space-y-3">
                <div className="space-y-1.5">
                  <label htmlFor="source-title" className="text-xs font-medium text-zinc-400">
                    Title{urlMode && <span className="text-zinc-600"> (optional)</span>}
                  </label>
                  <input
                    id="source-title"
                    type="text"
                    value={title}
                    onChange={(e) => setTitle(e.target.value)}
                    placeholder={urlMode ? 'From the video if left blank' : 'Track title'}
                    className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-500"
                  />
                </div>
                <div className="space-y-1.5">
                  <label htmlFor="source-artist" className="text-xs font-medium text-zinc-400">
                    Artist{urlMode && <span className="text-zinc-600"> (optional)</span>}
                  </label>
                  <input
                    id="source-artist"
                    type="text"
                    value={artist}
                    onChange={(e) => {
                      setArtist(e.target.value);
                      // Once the user types an artist the quiet guess hint
                      // has served its purpose.
                      if (e.target.value.trim()) setArtistUncertain(false);
                    }}
                    placeholder={urlMode ? 'From the video if left blank' : 'Artist name'}
                    className="w-full rounded-md border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:outline-none focus:border-zinc-500"
                  />
                  {artistUncertain && !urlMode && (
                    <p className="text-xs text-zinc-500">
                      Couldn't tell the artist from the file name
                    </p>
                  )}
                </div>
              </div>
            )}

            {error && (
              <div className="flex items-center gap-2 text-red-400 text-sm">
                <AlertCircle className="h-4 w-4 shrink-0" />
                {error}
              </div>
            )}

            <DialogFooter>
              <Button
                onClick={handleSubmit}
                disabled={!canSubmit}
                className="w-full"
              >
                {loading
                  ? <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                  : urlMode
                    ? <LinkIcon className="mr-2 h-4 w-4" />
                    : <Upload className="mr-2 h-4 w-4" />}
                {loading
                  ? (urlMode ? 'Submitting...' : 'Uploading...')
                  : (urlMode ? 'Fetch & Split Stems' : 'Split Stems')}
              </Button>
            </DialogFooter>
          </div>
        ) : (
          // Progress Display
          <div className="py-6 space-y-6">
            <div className="space-y-3">
              {STEPS.map((step, idx) => {
                const isComplete = idx < currentStepIndex;
                const isCurrent = idx === currentStepIndex;
                const isPending = idx > currentStepIndex;
                const isFailed = jobStatus.status === 'failed' && isCurrent;

                return (
                  <div
                    key={step.status}
                    className={cn(
                      "flex items-start gap-3 text-sm transition-colors",
                      isComplete && "text-green-400",
                      isCurrent && !isFailed && "text-white font-medium",
                      isFailed && "text-red-400",
                      isPending && "text-zinc-600"
                    )}
                  >
                    <div className={cn(
                      "h-6 w-6 shrink-0 rounded-full flex items-center justify-center border",
                      isComplete && "bg-green-500/20 border-green-500",
                      isCurrent && !isFailed && "bg-white/10 border-white animate-pulse",
                      isFailed && "bg-red-500/20 border-red-500",
                      isPending && "bg-zinc-800 border-zinc-700"
                    )}>
                      {isComplete && <Check className="h-3 w-3" />}
                      {isCurrent && !isFailed && <Loader2 className="h-3 w-3 animate-spin" />}
                      {isFailed && <X className="h-3 w-3" />}
                    </div>
                    <div className="min-w-0 flex-1 pt-0.5">
                      <span>{stepLabel(step.status, step.label, jobStatus)}</span>
                      {isCurrent && !isFailed && jobStatus.workerPhase && (
                        <p className="mt-0.5 truncate text-xs font-normal text-zinc-500" title={jobStatus.workerPhase}>
                          {jobStatus.workerPhase}
                        </p>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>

            {error && (
              <div className="p-3 bg-red-500/10 border border-red-500/30 rounded-lg text-sm text-red-400">
                <p className="font-medium">Processing Failed</p>
                <p className="text-red-400/80 mt-1">{error}</p>
              </div>
            )}

            {jobId && (
              <div className="text-xs text-zinc-600 font-mono">
                Job ID: {jobId}
              </div>
            )}

            <DialogFooter>
              {error ? (
                <Button onClick={handleRetry} variant="outline" className="w-full">
                  Try Again
                </Button>
              ) : jobStatus.status === 'ready' ? (
                <div className="w-full space-y-2">
                  {downloadUrl && (
                    <Button asChild className="w-full">
                      <a href={downloadUrl} download>
                        <Download className="mr-2 h-4 w-4" />
                        Download Multi-Track
                      </a>
                    </Button>
                  )}
                  <Button onClick={handleClose} variant="ghost" className="w-full">
                    <Check className="mr-2 h-4 w-4" />
                    Done
                  </Button>
                </div>
              ) : (
                <Button onClick={handleClose} variant="ghost" className="w-full">
                  Run in Background
                </Button>
              )}
            </DialogFooter>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
};
