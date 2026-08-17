import { useState } from 'react';
import { PasscodeGate } from '@/components/auth/PasscodeGate';
import { hasToken } from '@/lib/auth';
import { useRemoteSync } from '@/hooks/useRemoteSync';
import { MixerSurface } from '@/components/mixer/MixerSurface';
import { useMixerReset } from '@/hooks/useMixerReset';
import { useStore } from '@/stores/useStore';
import { Button } from '@/components/ui/button';
import { Slider } from '@/components/ui/slider';
import { RotateCcw, Volume2, Wifi, WifiOff } from 'lucide-react';
import { Toaster } from 'sonner';

/**
 * Touch control surface (/remote): the full mixer, no video, no audio.
 * Fader/mute/solo/master moves publish commands over the remote relay; the
 * playing browser applies them. Until a control is touched this page has no
 * effect on the main screen at all.
 */
const RemoteControls: React.FC = () => {
  const { connected, remoteTrackTitle } = useRemoteSync('remote');
  const { volume, setVolume } = useStore();
  const handleReset = useMixerReset();

  return (
    <div className="mx-auto flex h-full max-w-xl flex-col gap-6 p-6">
      <header className="flex items-center justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-zinc-100">Shizzle Remote</h1>
          <p className="mt-1 text-sm text-zinc-500" data-testid="remote-track">
            {remoteTrackTitle ? `Playing: ${remoteTrackTitle}` : 'No track reported yet'}
          </p>
        </div>
        <div
          className={`flex items-center gap-2 text-sm ${connected ? 'text-green-400' : 'text-red-400'}`}
          data-testid="remote-connection"
        >
          {connected ? <Wifi className="h-4 w-4" /> : <WifiOff className="h-4 w-4" />}
          {connected ? 'Connected' : 'Reconnecting…'}
        </div>
      </header>

      <div className="flex-1 overflow-y-auto pr-1">
        <MixerSurface size="touch" />
      </div>

      <footer className="space-y-4 border-t border-zinc-800 pt-4">
        <div className="flex items-center gap-4">
          <Volume2 className="h-5 w-5 shrink-0 text-zinc-400" />
          <Slider
            aria-label="Master volume"
            value={[Math.round(volume * 100)]}
            min={0}
            max={100}
            step={1}
            onValueChange={(vals) => setVolume(vals[0] / 100)}
            className="[&_.bg-primary]:bg-zinc-100 [&_.border-primary]:border-zinc-100 [&_[role=slider]]:h-7 [&_[role=slider]]:w-7"
          />
          <span className="w-10 text-right font-mono text-xs text-zinc-500">
            {Math.round(volume * 100)}%
          </span>
        </div>
        <Button
          variant="outline"
          className="w-full border-zinc-700 hover:bg-zinc-800"
          onClick={handleReset}
        >
          <RotateCcw className="mr-2 h-4 w-4" />
          Reset mix
        </Button>
      </footer>
    </div>
  );
};

export const RemoteMixerPage: React.FC = () => {
  const [authed, setAuthed] = useState<boolean>(() => hasToken());

  return (
    <div className="relative h-full w-full bg-black text-white antialiased">
      {authed ? <RemoteControls /> : <PasscodeGate onAuthed={() => setAuthed(true)} />}
      <Toaster theme="dark" position="bottom-left" />
    </div>
  );
};
