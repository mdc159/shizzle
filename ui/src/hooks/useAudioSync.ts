/**
 * useAudioSync Hook — thin React adapter over the PlaybackEngine.
 *
 * Responsibilities (all heavy lifting lives in the engine):
 * - Load stems into the engine when the manifest changes
 * - Forward play/pause/seek between video element, store, and engine
 * - Forward stem gain/mute/solo and master volume from the store
 * - Surface engine stall bailouts as store state + a toast
 */

import { useEffect, useCallback, useState } from 'react';
import { useStore } from '@/stores/useStore';
import { playbackEngine } from '@/lib/playback/mediaElementEngine';
import { linearToDb } from '@/lib/playback/db';
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
    volume,
    stemGains,
    stemMutes,
    stemSolos,
    setPlaying,
  } = useStore();

  const [isReady, setIsReady] = useState(false);
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

    const loadStems = async () => {
      try {
        const basePath = trackPublicUrl || `/videos/${trackSlug}`;
        await playbackEngine.load(manifest, basePath);
        setIsReady(true);
      } catch (err) {
        console.error('Failed to load stems:', err);
        setIsReady(false);
        toast.error('Failed to load audio stems. Try refreshing.');
      }
    };

    loadStems();

    return () => {
      playbackEngine.unload();
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

  // Sync play/pause state — controls BOTH video and engine
  useEffect(() => {
    if (!isReady) return;

    const video = videoRef.current;
    playbackEngine.attachVideo(video ?? null);

    if (playing) {
      Promise.all([video?.play(), playbackEngine.play()].filter(Boolean)).catch((err) => {
        console.error('Playback failed:', err);
        setPlaying(false);
        toast.error('Playback failed. Try again.');
      });
    } else {
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
  const handlePlay = useCallback(() => setPlaying(true), [setPlaying]);
  const handlePause = useCallback(() => setPlaying(false), [setPlaying]);

  return {
    isReady,
    handleSeek,
    handlePlay,
    handlePause,
  };
}
