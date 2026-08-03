/**
 * useAudioSync Hook
 *
 * Manages synchronization between video playback and audio stems.
 * - Loads stems when manifest changes
 * - Syncs play/pause between video and audioManager
 * - Applies stem gains from store to audioManager
 * - Handles periodic drift correction
 */

import { useEffect, useRef, useCallback, useState } from 'react';
import { useStore } from '@/stores/useStore';
import { audioManager, StemAudioManager } from '@/lib/audio';
import { toast } from 'sonner';
import type { StemId } from '@/types/karaoke';

interface UseAudioSyncOptions {
  videoRef: React.RefObject<HTMLVideoElement | null>;
}

interface UseAudioSyncReturn {
  isReady: boolean;
  handleSeek: (time: number) => void;
  handlePlay: () => void;
  handlePause: () => void;
}

export function useAudioSync({ videoRef }: UseAudioSyncOptions): UseAudioSyncReturn {
  const {
    manifest,
    currentTrack,
    playing,
    stemGains,
    stemMutes,
    stemSolos,
    setPlaying,
  } = useStore();

  const [isReady, setIsReady] = useState(false);
  const syncIntervalRef = useRef<number | null>(null);
  const lastVideoTimeRef = useRef(0);
  const stalledTicksRef = useRef(0);
  const STALL_TICKS_THRESHOLD = 3;
  const SOFT_DRIFT_SEC = 0.05;
  const HARD_DRIFT_SEC = 0.25;

  const stopSyncInterval = useCallback(() => {
    if (syncIntervalRef.current) {
      clearInterval(syncIntervalRef.current);
      syncIntervalRef.current = null;
    }
  }, []);

  // Periodic sync to handle drift
  const startSyncInterval = useCallback(() => {
    stopSyncInterval();
    syncIntervalRef.current = window.setInterval(() => {
      const video = videoRef.current;
      if (!video || !isReady || !playing) return;

      const currentVideoTime = video.currentTime;
      const advanced = currentVideoTime > lastVideoTimeRef.current + 0.02;
      lastVideoTimeRef.current = currentVideoTime;

      // If video is not advancing while we think we're playing, avoid repeated rewinds.
      if (!advanced) {
        stalledTicksRef.current += 1;
        if (stalledTicksRef.current >= STALL_TICKS_THRESHOLD) {
          audioManager.pause();
          audioManager.setPlaybackRate(1);
          setPlaying(false);
          toast.error('Video playback stalled. Press play to resume.');
          stalledTicksRef.current = 0;
        }
        return;
      }
      stalledTicksRef.current = 0;

      const audioTime = audioManager.getAverageCurrentTime();
      const drift = audioTime - currentVideoTime;
      const absDrift = Math.abs(drift);

      if (absDrift < SOFT_DRIFT_SEC) {
        audioManager.setPlaybackRate(1);
        return;
      }

      if (absDrift < HARD_DRIFT_SEC) {
        // Gentle correction: slightly speed up / slow down stems to converge.
        const nudge = Math.max(-0.005, Math.min(0.005, -drift * 0.03));
        audioManager.setPlaybackRate(1 + nudge);
        return;
      }

      // Severe drift: single hard correction, then continue with soft mode.
      audioManager.syncToVideoTime(currentVideoTime, HARD_DRIFT_SEC);
      audioManager.setPlaybackRate(1);
    }, 1000);
  }, [videoRef, isReady, playing, setPlaying, stopSyncInterval]);

  // Load stems when manifest changes
  useEffect(() => {
    if (!manifest || !currentTrack) {
      // Deliberate reset when the track/manifest goes away; restructuring this
      // hook is deferred to the Phase 1.3 PlaybackEngine extraction.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setIsReady(false);
      return;
    }

    const loadStems = async () => {
      try {
        const basePath = currentTrack.publicUrl || `/videos/${currentTrack.slug}`;
        await audioManager.loadStems(basePath, manifest.stems);
        setIsReady(true);
      } catch (err) {
        console.error('Failed to load stems:', err);
        setIsReady(false);
        toast.error('Failed to load audio stems. Try refreshing.');
      }
    };

    loadStems();

    return () => {
      audioManager.cleanup();
      setIsReady(false);
    };
  }, [manifest, currentTrack?.slug]);

  // Sync play/pause state — controls BOTH video and audio
  useEffect(() => {
    if (!isReady) return;

    const video = videoRef.current;

    if (playing) {
      if (video) {
        audioManager.syncToVideoTime(video.currentTime, HARD_DRIFT_SEC);
        lastVideoTimeRef.current = video.currentTime;
      }
      Promise.all([video?.play(), audioManager.play()].filter(Boolean)).catch((err) => {
        console.error('Playback failed:', err);
        setPlaying(false);
        toast.error('Playback failed. Try again.');
      });
      startSyncInterval();
    } else {
      video?.pause();
      audioManager.pause();
      audioManager.setPlaybackRate(1);
      stopSyncInterval();
    }
  }, [playing, isReady, videoRef, startSyncInterval, stopSyncInterval, setPlaying]);

  // Apply gain changes from store to audio engine (includes mute/solo logic)
  useEffect(() => {
    if (!isReady) return;

    const anySoloed = Object.values(stemSolos).some(Boolean);

    Object.entries(stemGains).forEach(([stem, dbValue]) => {
      const stemId = stem as StemId;
      const isMuted = stemMutes[stemId];
      const isSoloed = stemSolos[stemId];

      // Muted, or another stem is soloed and this one isn't → silence
      const shouldSilence = isMuted || (anySoloed && !isSoloed);
      const linearGain = shouldSilence ? 0 : StemAudioManager.dbToLinear(dbValue);
      audioManager.setGain(stemId, linearGain);
    });
  }, [stemGains, stemMutes, stemSolos, isReady]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      stopSyncInterval();
      audioManager.cleanup();
    };
  }, [stopSyncInterval]);

  // Seek handler - syncs both video and audio
  const handleSeek = useCallback((time: number) => {
    if (videoRef.current) {
      videoRef.current.currentTime = time;
    }
    if (isReady) {
      audioManager.seek(time);
    }
  }, [videoRef, isReady]);

  // Play/pause handlers — just toggle state, Effect 2 does the work
  const handlePlay = useCallback(() => setPlaying(true), [setPlaying]);
  const handlePause = useCallback(() => setPlaying(false), [setPlaying]);

  return {
    isReady,
    handleSeek,
    handlePlay,
    handlePause,
  };
}
