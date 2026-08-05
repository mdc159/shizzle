import React, { useEffect, useState } from 'react';
import { Play, Pause, Maximize, ListMusic, AudioWaveform, Loader2 } from 'lucide-react';
import { useStore } from '@/stores/useStore';
import { Button } from '@/components/ui/button';
import { Slider } from '@/components/ui/slider';
import { cn } from '@/lib/utils';
import { playbackEngine } from '@/lib/playback/mediaElementEngine';

interface TransportControlsProps {
  ready: boolean;
  onPlay: () => Promise<void>;
  onPause: () => void;
  onSeek: (time: number) => void;
}

export const TransportControls: React.FC<TransportControlsProps> = ({ ready, onPlay, onPause, onSeek }) => {
  const {
    playing,
    currentTime,
    duration,
    volume,
    setVolume,
    controlsVisible,
    currentTrack,
    activeDrawer,
    setActiveDrawer
  } = useStore();

  // Master-limiter activity indicator: poll engine metrics while playing.
  const [limiterActive, setLimiterActive] = useState(false);
  useEffect(() => {
    if (!playing) return;
    const id = window.setInterval(() => {
      setLimiterActive(playbackEngine.getMetrics().limiter.active);
    }, 250);
    return () => {
      window.clearInterval(id);
    };
  }, [playing]);
  const showLimiter = playing && limiterActive;

  const formatTime = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = Math.floor(seconds % 60);
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  return (
    <div className={cn(
      "fixed bottom-0 left-0 right-0 p-6 bg-gradient-to-t from-black/90 to-transparent transition-opacity duration-300 z-40",
      controlsVisible ? "opacity-100" : "opacity-0 pointer-events-none"
    )}>
      <div className="max-w-screen-xl mx-auto space-y-4">
        {/* Seek Bar */}
        <div className="flex items-center gap-3 text-sm font-mono text-zinc-300">
          <span>{formatTime(currentTime)}</span>
          <Slider
            aria-label="Seek"
            value={[currentTime]}
            max={duration || 100}
            step={1}
            onValueChange={(vals) => onSeek(vals[0])}
            className="cursor-pointer"
          />
          <span>{formatTime(duration)}</span>
        </div>

        <div className="flex items-center justify-between">
          {/* Left: Track Info & Drawers */}
          <div className="flex items-center gap-4">
            <Button
              variant="ghost"
              size="icon"
              className={cn(activeDrawer === 'library' && "bg-accent text-accent-foreground")}
              onClick={() => setActiveDrawer(activeDrawer === 'library' ? 'none' : 'library')}
              aria-label="Library"
            >
              <ListMusic className="h-5 w-5" />
            </Button>

            <div className="hidden md:block">
              {currentTrack ? (
                <div className="flex flex-col">
                  <span className="text-sm font-semibold text-white">{currentTrack.title}</span>
                  <span className="text-xs text-zinc-400">{currentTrack.artist}</span>
                </div>
              ) : (
                <span className="text-sm text-zinc-500 italic">No track loaded</span>
              )}
            </div>
          </div>

          {/* Center: Play Transport */}
          <div className="flex items-center gap-4">
            <Button
              size="icon"
              variant="outline"
              className="h-12 w-12 rounded-full border-zinc-700 bg-black/50 hover:bg-white hover:text-black transition-all"
              onClick={() => {
                if (playing) onPause();
                else void onPlay();
              }}
              disabled={!currentTrack || !ready}
              aria-label={!ready && currentTrack ? 'Loading stems' : playing ? 'Pause' : 'Play'}
            >
              {!ready && currentTrack ? (
                <Loader2 className="h-6 w-6 animate-spin" />
              ) : playing ? (
                <Pause className="h-6 w-6 fill-current" />
              ) : (
                <Play className="h-6 w-6 fill-current ml-1" />
              )}
            </Button>
          </div>

          {/* Right: Volume & Mixer & Fullscreen */}
          <div className="flex items-center gap-4">
            <div className="flex items-center gap-2 w-32 group">
              <Button
                variant="ghost"
                size="icon"
                className={cn(activeDrawer === 'mixer' && "bg-accent text-accent-foreground")}
                onClick={() => setActiveDrawer(activeDrawer === 'mixer' ? 'none' : 'mixer')}
                aria-label="Mixer"
              >
                <AudioWaveform className="h-5 w-5" />
              </Button>

              {/* Volume Slider - visible on group hover or always? Let's keep it visible for now */}
              <Slider
                aria-label="Master volume"
                value={[volume]}
                max={1}
                step={0.01}
                onValueChange={(vals) => setVolume(vals[0])}
                className="w-24"
              />

              {/* Master limiter activity (amber = gain reduction > 0.5 dB) */}
              <span
                title="Master limiter active"
                aria-label={showLimiter ? 'Master limiter active' : 'Master limiter idle'}
                className={cn(
                  'h-1.5 w-1.5 shrink-0 rounded-full transition-colors',
                  showLimiter ? 'bg-amber-400' : 'bg-zinc-800'
                )}
              />
            </div>

            <Button aria-label="Fullscreen" variant="ghost" size="icon" onClick={() => {
              if (!document.fullscreenElement) {
                document.documentElement.requestFullscreen();
              } else {
                document.exitFullscreen();
              }
            }}>
              <Maximize className="h-5 w-5" />
            </Button>
          </div>
        </div>
      </div>
    </div>
  );
};
