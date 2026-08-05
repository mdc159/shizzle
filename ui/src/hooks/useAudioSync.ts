/**
 * useAudioSync Hook — thin React adapter over the PlaybackEngine.
 *
 * Responsibilities (all heavy lifting lives in the engine):
 * - Load stems into the engine when the manifest changes
 * - Forward play/pause/seek between video element, store, and engine
 * - Forward stem gain/mute/solo and master volume from the store
 * - Surface engine stall bailouts as store state + a toast
 */

import { useEffect, useCallback, useRef, useState } from 'react';
import { useStore } from '@/stores/useStore';
import { playbackEngine } from '@/lib/playback/mediaElementEngine';
import { metricsDetail, playbackTelemetry } from '@/lib/playback/telemetry';
import { linearToDb } from '@/lib/playback/db';
import { toast } from 'sonner';
import type { StemId } from '@/types/karaoke';

interface UseAudioSyncOptions {
  videoRef: React.RefObject<HTMLVideoElement | null>;
}

interface UseAudioSyncReturn {
  isReady: boolean;
  handleSeek: (time: number) => void;
  handlePlay: () => Promise<void>;
  handlePause: () => void;
}

export function useAudioSync({ videoRef }: UseAudioSyncOptions): UseAudioSyncReturn {
  const {
    manifest,
    currentTrack,
    playing,
    volume,
    stemGains,
    stemMutes,
    stemSolos,
    setPlaying,
  } = useStore();

  const [isReady, setIsReady] = useState(false);
  const playAttemptRef = useRef(0);
  const trackSlug = currentTrack?.slug;
  const trackPublicUrl = currentTrack?.publicUrl;

  // Load stems into the engine when manifest changes
  useEffect(() => {
    if (!manifest || !trackSlug) {
      // Deliberate reset when the track/manifest goes away.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setIsReady(false);
      return;
    }

    let cancelled = false;
    const loadStems = async () => {
      try {
        const basePath = trackPublicUrl || `/videos/${trackSlug}`;
        await playbackEngine.load(manifest, basePath);
        if (!cancelled) {
          playbackTelemetry.start(manifest.track_id || trackSlug, manifest.generation || 1);
          setIsReady(true);
        }
      } catch (err) {
        if (cancelled) return;
        console.error('Failed to load stems:', err);
        setIsReady(false);
        toast.error('Failed to load audio stems. Try refreshing.');
      }
    };

    loadStems();

    return () => {
      cancelled = true;
      playbackEngine.unload();
      playbackTelemetry.end('track-unloaded');
      setIsReady(false);
    };
  }, [manifest, trackSlug, trackPublicUrl]);

  // Surface engine stall bailouts (video not advancing) to the UI
  useEffect(() => {
    playbackEngine.onStallBailout(() => {
      setPlaying(false);
      toast.error('Video playback stalled. Press play to resume.');
    });
    return () => {
      playbackEngine.onStallBailout(null);
    };
  }, [setPlaying]);

  useEffect(() => {
    playbackEngine.onIncident((incident) => {
      playbackTelemetry.incident(incident, playbackEngine.getMetrics());
      if (incident.code === 'recovery-failed') {
        toast.error('Playback recovery failed. Tap Play to resume.');
      } else if (incident.code === 'stem-media-error') {
        toast.error('An audio stem failed to decode. Playback needs repair.');
      }
    });
    return () => playbackEngine.onIncident(null);
  }, []);

  // Persist low-rate direct health evidence only while the page is visible and
  // playback is actually requested. Incidents are emitted immediately above.
  useEffect(() => {
    if (!isReady || !playing) return;
    const heartbeat = window.setInterval(() => {
      if (document.visibilityState === 'visible') {
        playbackTelemetry.send('heartbeat', metricsDetail(playbackEngine.getMetrics()));
      }
    }, 15_000);
    return () => window.clearInterval(heartbeat);
  }, [isReady, playing]);

  useEffect(() => {
    const onVisibility = () => {
      playbackTelemetry.send(
        document.visibilityState === 'visible' ? 'visibility-visible' : 'visibility-hidden',
        metricsDetail(playbackEngine.getMetrics()),
      );
    };
    document.addEventListener('visibilitychange', onVisibility);
    return () => document.removeEventListener('visibilitychange', onVisibility);
  }, []);

  useEffect(() => {
    const video = videoRef.current;
    if (!video || !isReady) return;
    const onSeeked = () =>
      playbackTelemetry.send('seek-settled', metricsDetail(playbackEngine.getMetrics()));
    const onEnded = () =>
      playbackTelemetry.send('ended', metricsDetail(playbackEngine.getMetrics()));
    video.addEventListener('seeked', onSeeked);
    video.addEventListener('ended', onEnded);
    return () => {
      video.removeEventListener('seeked', onSeeked);
      video.removeEventListener('ended', onEnded);
    };
  }, [isReady, videoRef]);

  // Sync play/pause state — controls BOTH video and engine
  useEffect(() => {
    if (!isReady) return;

    const video = videoRef.current;
    playbackEngine.attachVideo(video ?? null);

    if (!playing) {
      video?.pause();
      playbackEngine.pause();
    }
  }, [playing, isReady, videoRef, setPlaying]);

  // Forward stem gains/mutes/solos from store to engine
  useEffect(() => {
    if (!isReady) return;

    (Object.keys(stemGains) as StemId[]).forEach((stemId) => {
      playbackEngine.setStemGainDb(stemId, stemGains[stemId]);
      playbackEngine.setStemMute(stemId, stemMutes[stemId]);
      playbackEngine.setStemSolo(stemId, stemSolos[stemId]);
    });
  }, [stemGains, stemMutes, stemSolos, isReady]);

  // Master volume: slider is linear 0..1, engine wants dB (headroom is the
  // engine's own concern). volume=1 -> 0 dB user gain.
  useEffect(() => {
    playbackEngine.setMasterGainDb(volume <= 0 ? -Infinity : linearToDb(volume));
  }, [volume]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      playbackEngine.unload();
    };
  }, []);

  // Seek handler - syncs both video and engine
  const handleSeek = useCallback(
    (time: number) => {
      playbackTelemetry.send('seek-started', { targetSeconds: time });
      if (videoRef.current) {
        videoRef.current.currentTime = time;
      }
      if (isReady) {
        playbackEngine.seek(time);
      }
    },
    [videoRef, isReady],
  );

  // Play/pause handlers — just toggle state, the play/pause effect does the work
  const handlePlay = useCallback(async () => {
    if (!isReady) {
      toast.error('Stems are still loading. Try again in a moment.');
      return;
    }
    const attempt = ++playAttemptRef.current;
    const video = videoRef.current;
    const replaying = Boolean(video?.ended);
    playbackEngine.attachVideo(video ?? null);
    if (replaying && video) {
      // Browsers implicitly rewind an ended video when play() is called, but
      // that happens after the engine's startup alignment. Rewind the master
      // and every stem explicitly first so replay cannot lose its opening
      // seconds while recovering from end-of-track alignment.
      video.currentTime = 0;
      playbackEngine.seek(0);
    }
    setPlaying(true);
    try {
      // Both calls are created directly in the click/touch handler so iPad's
      // transient media activation covers the video, AudioContext, and stems.
      const starts = [playbackEngine.play()];
      if (video) starts.unshift(video.play());
      await Promise.all(starts);
      playbackTelemetry.send(
        replaying ? 'replay' : 'playing',
        metricsDetail(playbackEngine.getMetrics()),
      );
    } catch (err) {
      if (attempt !== playAttemptRef.current) return;
      console.error('Playback failed:', err);
      video?.pause();
      playbackEngine.pause();
      setPlaying(false);
      toast.error(err instanceof Error ? err.message : 'Playback failed. Tap Play to retry.');
    }
  }, [isReady, setPlaying, videoRef]);
  const handlePause = useCallback(() => {
    playAttemptRef.current += 1;
    // Invalidate the engine command before the video emits `pause`, so the
    // watchdog can distinguish this intentional stop from a spontaneous one.
    playbackEngine.pause();
    videoRef.current?.pause();
    setPlaying(false);
    playbackTelemetry.send('pause', metricsDetail(playbackEngine.getMetrics()));
  }, [setPlaying, videoRef]);

  return {
    isReady,
    handleSeek,
    handlePlay,
    handlePause,
  };
}
