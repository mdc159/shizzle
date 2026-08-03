import React, { useEffect, useRef, useCallback, useState } from 'react';
import { useStore } from '@/stores/useStore';
import { TransportControls } from './TransportControls';
import { cn } from '@/lib/utils';
import { useAudioSync } from '@/hooks/useAudioSync';
import { loadManifest } from '@/lib/api';
import { resolveMediaUrl } from '@/lib/playback/mediaUrl';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';

export const PlayerShell: React.FC = () => {
  const videoRef = useRef<HTMLVideoElement>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [isLoading, setIsLoading] = useState(false);

  const {
    currentTrack,
    manifest,
    setManifest,
    playing,
    setPlaying,
    setControlsVisible,
    controlsVisible,
    currentTime,
    setCurrentTime,
    setDuration,
    activeDrawer
  } = useStore();

  // Audio sync hook handles stem loading and synchronization
  const { handleSeek } = useAudioSync({ videoRef });

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
        // Server resolves media refs (same-origin /cdn for cloud, relative for local).
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

  // Build video URL from manifest. Cloud manifests carry a same-origin /cdn
  // path; local manifests carry a relative path joined with the track base.
  const videoSrc = currentTrack && manifest
    ? resolveMediaUrl(currentTrack.publicUrl || `/videos/${currentTrack.slug}`, manifest.video)
    : undefined;

  return (
    <div
      ref={containerRef}
      className="absolute inset-0 bg-black overflow-hidden"
      onMouseMove={showControls}
      onTouchStart={showControls}
    >
      {/* Background Video Layer */}
      {currentTrack ? (
        <>
          {isLoading ? (
            <div className="absolute inset-0 flex items-center justify-center bg-zinc-950">
              <div className="text-center space-y-4">
                <Loader2 className="h-12 w-12 animate-spin text-zinc-400 mx-auto" />
                <p className="text-zinc-500">Loading stems...</p>
              </div>
            </div>
          ) : (
            <video
              ref={videoRef}
              className="w-full h-full object-contain"
              src={videoSrc}
              muted // Audio comes from stems, not video
              onTimeUpdate={handleTimeUpdate}
              onEnded={() => setPlaying(false)}
              onError={() => {
                setPlaying(false);
                toast.error('Video failed to decode. Re-process this track for browser-safe playback.');
              }}
              onStalled={() => {
                setPlaying(false);
                toast.error('Video stalled. Press play to retry.');
              }}
              onLoadedMetadata={() => {
                if (videoRef.current) {
                  setDuration(videoRef.current.duration);
                }
              }}
              loop={false}
              playsInline
              crossOrigin="anonymous"
            />
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

      <TransportControls />
    </div>
  );
};
