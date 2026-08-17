import React, { useEffect, useRef, useCallback, useState } from 'react';
import { useStore } from '@/stores/useStore';
import { TransportControls } from './TransportControls';
import { PipelineDrawer } from '@/components/pipeline/PipelineDrawer';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';
import { useAudioSync } from '@/hooks/useAudioSync';
import { loadManifest } from '@/lib/api';
import { resolveMediaUrl } from '@/lib/playback/mediaUrl';
import { Activity, LayoutDashboard, Loader2, SlidersHorizontal } from 'lucide-react';
import { toast } from 'sonner';

export const PlayerShell: React.FC = () => {
  const videoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [isBufferingVideo, setIsBufferingVideo] = useState(false);
  const [bufferedVideoSrc, setBufferedVideoSrc] = useState<string | undefined>();
  const [stagedVideoBytes, setStagedVideoBytes] = useState(0);

  const {
    currentTrack,
    manifest,
    setManifest,
    playing,
    setControlsVisible,
    controlsVisible,
    currentTime,
    setCurrentTime,
    setDuration,
    activeDrawer,
    setActiveDrawer
  } = useStore();

  // Audio sync hook handles stem loading and synchronization
  const { isReady, handleSeek, handlePlay, handlePause } = useAudioSync({ videoRef });

  const hideTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Auto-hide controls logic
  const showControls = useCallback(() => {
    setControlsVisible(true);
    if (hideTimeoutRef.current) clearTimeout(hideTimeoutRef.current);

    // Only auto-hide if no drawer is active
    if (activeDrawer === 'none' && playing) {
      hideTimeoutRef.current = setTimeout(() => {
        setControlsVisible(false);
      }, 2500);
    }
  }, [activeDrawer, playing, setControlsVisible]);

  useEffect(() => {
    if (!playing) {
      setControlsVisible(true);
      if (hideTimeoutRef.current) clearTimeout(hideTimeoutRef.current);
    } else {
      showControls();
    }
  }, [playing, showControls, setControlsVisible]);

  const trackSlug = currentTrack?.slug;

  // Load manifest when track changes
  useEffect(() => {
    if (!trackSlug) {
      setManifest(null);
      return;
    }

    const fetchManifest = async () => {
      setIsLoading(true);
      try {
        // The authenticated server resolves cloud media refs to exact-object,
        // expiring CloudFront URLs. Local manifests remain relative.
        const manifestData = await loadManifest(trackSlug);
        setManifest(manifestData);
        setDuration(manifestData.duration);
      } catch (err) {
        console.error('Failed to load manifest:', err);
        toast.error('Failed to load track stems');
        setManifest(null);
      } finally {
        setIsLoading(false);
      }
    };

    fetchManifest();
  }, [trackSlug, setManifest, setDuration]);

  // Handle Time Updates - sync store with video time
  const handleTimeUpdate = useCallback(() => {
    if (videoRef.current) {
      setCurrentTime(videoRef.current.currentTime);
    }
  }, [setCurrentTime]);

  // Handle Seek from transport controls
  useEffect(() => {
    const video = videoRef.current;
    if (video && Math.abs(video.currentTime - currentTime) > 1) {
      handleSeek(currentTime);
    }
  }, [currentTime, handleSeek]);

  const requestSeek = useCallback((time: number) => {
    setCurrentTime(time);
    handleSeek(time);
  }, [handleSeek, setCurrentTime]);

  // Build video URL from manifest. Cloud manifests carry a direct signed edge
  // URL; local manifests carry a relative path joined with the track base.
  const videoSrc = currentTrack && manifest
    ? resolveMediaUrl(currentTrack.publicUrl || `/videos/${currentTrack.slug}`, manifest.video)
    : undefined;

  // The video is the authoritative clock for seven independently decoded
  // media streams. Natural library testing showed Chromium could deprioritize
  // a 66 MB video Range for ~15 s even through direct CloudFront while all six
  // audio streams remained healthy. Stage only the bounded, audio-less video
  // as a revocable Blob before enabling Play; stems continue to stream. The
  // current library's largest video is 80.3 MB. The ceiling prevents a future
  // unbounded import from recreating the historic whole-file RAM failure.
  useEffect(() => {
    setBufferedVideoSrc(undefined);
    setStagedVideoBytes(0);
    if (!videoSrc || !isReady) {
      setIsBufferingVideo(false);
      return;
    }

    const controller = new AbortController();
    let objectUrl: string | undefined;
    setIsBufferingVideo(true);

    const bufferVideo = async () => {
      try {
        const response = await fetch(videoSrc, {
          signal: controller.signal,
          // Same-origin cookies must ride: CloudFront signed cookies on /cdn,
          // the device-token cookie on local-profile /api/tracks paths.
          credentials: 'same-origin',
        });
        if (!response.ok) throw new Error(`Video download failed with HTTP ${response.status}`);
        const declaredBytes = Number(response.headers.get('content-length') ?? 0);
        const maxBytes = 128 * 1024 * 1024;
        if (declaredBytes > maxBytes) {
          throw new Error('Video exceeds the 128 MB browser-staging limit');
        }
        const blob = await response.blob();
        if (blob.size > maxBytes) throw new Error('Video exceeds the 128 MB browser-staging limit');
        if (controller.signal.aborted) return;
        objectUrl = URL.createObjectURL(blob);
        setStagedVideoBytes(blob.size);
        setBufferedVideoSrc(objectUrl);
      } catch (error) {
        if (controller.signal.aborted) return;
        console.error('Failed to stage master video:', error);
        toast.error(error instanceof Error ? error.message : 'Failed to stage master video');
      } finally {
        if (!controller.signal.aborted) setIsBufferingVideo(false);
      }
    };

    void bufferVideo();
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [videoSrc, isReady]);

  const mediaReady = isReady && Boolean(bufferedVideoSrc) && !isBufferingVideo;

  return (
    <div
      ref={containerRef}
      className="absolute inset-0 bg-black overflow-hidden"
      onMouseMove={showControls}
      onTouchStart={showControls}
    >
      <div className="fixed right-6 top-6 z-40 flex items-center gap-2">
        <Button
          variant="ghost"
          className="gap-2 bg-black/50 text-zinc-300"
          asChild
        >
          <a href="/remote" target="_blank" rel="noopener noreferrer" aria-label="Open remote mixer in a new tab">
            <SlidersHorizontal className="h-5 w-5" />
            Remote
          </a>
        </Button>
        <Button
          variant="ghost"
          className="gap-2 bg-black/50 text-zinc-300"
          asChild
        >
          <a href="/dashboard" target="_blank" rel="noopener noreferrer" aria-label="Open pipeline dashboard in a new tab">
            <LayoutDashboard className="h-5 w-5" />
            Dashboard
          </a>
        </Button>
        <Button
          variant="ghost"
          className={cn(
            'gap-2 bg-black/50 text-zinc-300',
            activeDrawer === 'pipeline' && 'bg-accent text-accent-foreground'
          )}
          onClick={() => setActiveDrawer(activeDrawer === 'pipeline' ? 'none' : 'pipeline')}
          aria-label="Pipeline"
        >
          <Activity className="h-5 w-5" />
          Pipeline
        </Button>
      </div>
      <PipelineDrawer />

      {/* Background Video Layer */}
      {currentTrack ? (
        <>
          <video
            ref={videoRef}
            className="w-full h-full object-contain"
            src={bufferedVideoSrc}
            data-staged-bytes={stagedVideoBytes}
            preload="auto"
            muted // Audio comes from stems, not video
            onTimeUpdate={handleTimeUpdate}
            onEnded={handlePause}
            onError={() => {
              handlePause();
              toast.error('Video failed to decode. Re-process this track for browser-safe playback.');
            }}
            onStalled={() => {
              // The engine's direct watchdog owns buffering recovery. Do not
              // convert a transient decoder stall into a destructive pause.
              console.warn('Video transport reported a stalled event');
            }}
            onLoadedMetadata={() => {
              if (videoRef.current) {
                setDuration(videoRef.current.duration);
              }
            }}
            loop={false}
            playsInline
          />
          {(isLoading || isBufferingVideo) && (
            <div className="absolute inset-0 flex items-center justify-center bg-zinc-950">
              <div className="text-center space-y-4">
                <Loader2 className="h-12 w-12 animate-spin text-zinc-400 mx-auto" />
                <p className="text-zinc-500">
                  {isBufferingVideo ? 'Staging video for uninterrupted playback...' : 'Loading stems...'}
                </p>
              </div>
            </div>
          )}
        </>
      ) : (
        <div className="absolute inset-0 flex items-center justify-center bg-zinc-950">
          <div className="text-center space-y-4">
            <h1 className="text-4xl font-bold tracking-tighter text-zinc-100">Karaoke Mixer</h1>
            <p className="text-zinc-500">Select a track from the library to begin</p>
          </div>
        </div>
      )}

      {/* Overlay Gradient (for legibility) */}
      <div className={cn(
        "absolute inset-0 pointer-events-none transition-opacity duration-300",
        controlsVisible ? "bg-black/20" : "opacity-0"
      )} />

      <TransportControls
        ready={mediaReady}
        onPlay={handlePlay}
        onPause={handlePause}
        onSeek={requestSeek}
      />
    </div>
  );
};
