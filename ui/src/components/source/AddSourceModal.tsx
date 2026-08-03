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
import { Loader2, Check, X, AlertCircle, Upload, Download } from 'lucide-react';
import { uploadFile, getJobStatus } from '@/lib/api';
import { toast } from 'sonner';
import type { JobStatus } from '@/types/karaoke';
import { cn } from '@/lib/utils';

// Processing steps for progress display (Phase 2 job stages)
const STEPS = [
  { status: 'pending', label: 'Queued' },
  { status: 'downloading', label: 'Fetching Source' },
  { status: 'splitting', label: 'Separating Stems' },
  { status: 'verifying', label: 'Verifying' },
  { status: 'publishing', label: 'Publishing' },
  { status: 'ready', label: 'Complete' },
] as const;

// Polling configuration
const BASE_POLL_INTERVAL = 3000;
const MAX_POLL_INTERVAL = 30000;
const MAX_POLL_DURATION = 60 * 60 * 1000; // 1 hour (Demucs can be slow)

export const AddSourceModal: React.FC = () => {
  const { activeDrawer, setActiveDrawer, setActiveJob } = useStore();
  const isOpen = activeDrawer === 'source';

  const [selectedFile, setSelectedFile] = useState<File | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [jobStatus, setJobStatus] = useState<JobStatus | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const pollIntervalRef = useRef<number | null>(null);
  const pollStartTimeRef = useRef<number>(0);
  const consecutiveErrorsRef = useRef<number>(0);
  const currentPollIntervalRef = useRef<number>(BASE_POLL_INTERVAL);
  const fileInputRef = useRef<HTMLInputElement>(null);

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
      if (elapsed > MAX_POLL_DURATION) {
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
          setError(status.error || 'Processing failed');
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

  const startPolling = (id: string) => {
    stopPolling();
    pollStartTimeRef.current = Date.now();
    schedulePoll(id);
  };

  const handleFileSelect = useCallback((file: File) => {
    setSelectedFile(file);
    setError(null);
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
    if (!selectedFile) return;

    setLoading(true);
    setError(null);
    setJobStatus(null);

    try {
      const result = await uploadFile(selectedFile);
      setJobId(result.jobId);
      setJobStatus({ jobId: result.jobId, status: 'pending' });
      toast.success('Processing started');
      startPolling(result.jobId);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to upload');
      setLoading(false);
      toast.error('Failed to upload file');
    }
  };

  const handleClose = () => {
    stopPolling();
    setSelectedFile(null);
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

  const downloadUrl = jobId ? `/api/tracks/${jobId}/download` : null;

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && handleClose()}>
      <DialogContent className="sm:max-w-[425px] bg-zinc-950 border-zinc-800 text-zinc-100">
        <DialogHeader>
          <DialogTitle>Add Source</DialogTitle>
          <DialogDescription className="text-zinc-500">
            {jobStatus ? 'Processing your track...' : 'Drop a video file to split stems.'}
          </DialogDescription>
        </DialogHeader>

        {!jobStatus ? (
          // File Drop Zone
          <div className="space-y-4 py-4">
            <div
              className={cn(
                "border-2 border-dashed rounded-lg p-8 text-center transition-colors cursor-pointer",
                dragOver
                  ? "border-white bg-white/5"
                  : selectedFile
                    ? "border-green-500/50 bg-green-500/5"
                    : "border-zinc-700 hover:border-zinc-500"
              )}
              onDragOver={(e) => { e.preventDefault(); setDragOver(true); }}
              onDragLeave={() => setDragOver(false)}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
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

            {error && (
              <div className="flex items-center gap-2 text-red-400 text-sm">
                <AlertCircle className="h-4 w-4 shrink-0" />
                {error}
              </div>
            )}

            <DialogFooter>
              <Button
                onClick={handleSubmit}
                disabled={!selectedFile || loading}
                className="w-full"
              >
                {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Upload className="mr-2 h-4 w-4" />}
                {loading ? "Uploading..." : "Split Stems"}
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
                      "flex items-center gap-3 text-sm transition-colors",
                      isComplete && "text-green-400",
                      isCurrent && !isFailed && "text-white font-medium",
                      isFailed && "text-red-400",
                      isPending && "text-zinc-600"
                    )}
                  >
                    <div className={cn(
                      "h-6 w-6 rounded-full flex items-center justify-center border",
                      isComplete && "bg-green-500/20 border-green-500",
                      isCurrent && !isFailed && "bg-white/10 border-white animate-pulse",
                      isFailed && "bg-red-500/20 border-red-500",
                      isPending && "bg-zinc-800 border-zinc-700"
                    )}>
                      {isComplete && <Check className="h-3 w-3" />}
                      {isCurrent && !isFailed && <Loader2 className="h-3 w-3 animate-spin" />}
                      {isFailed && <X className="h-3 w-3" />}
                    </div>
                    <span>{step.label}</span>
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
