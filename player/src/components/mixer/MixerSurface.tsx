import React from 'react';
import { useStore } from '@/stores/useStore';
import { Slider } from '@/components/ui/slider';
import { Button } from '@/components/ui/button';
import type { StemId } from '@/types/karaoke';

const formatGain = (db: number) => {
  if (db <= -60) return '-∞';
  return `${db >= 0 ? '+' : ''}${db.toFixed(1)}`;
};

interface MixerSurfaceProps {
  /** 'touch' renders larger targets for tablet/phone control surfaces. */
  size?: 'default' | 'touch';
}

/**
 * The six stem strips (fader + solo + mute). Store-driven on every surface:
 * the local MixerDrawer and the /remote page render this same component, and
 * useAudioSync / useRemoteSync react to the store changes it makes.
 */
export const MixerSurface: React.FC<MixerSurfaceProps> = ({ size = 'default' }) => {
  const {
    stemGains, setStemGain,
    stemMutes, toggleStemMute,
    stemSolos, toggleStemSolo,
  } = useStore();

  const stems = Object.keys(stemGains) as StemId[];
  const touch = size === 'touch';

  return (
    <div className={touch ? 'space-y-8' : 'space-y-6'}>
      {stems.map(stem => (
        <div key={stem} className="space-y-2" data-testid={`stem-strip-${stem}`}>
          <div className={`flex items-center justify-between ${touch ? 'text-base' : 'text-sm'}`}>
            <div className="flex items-center gap-2">
              <span className="capitalize font-medium text-zinc-300">{stem}</span>
              <Button
                variant="ghost"
                size="sm"
                className={`${touch ? 'h-10 w-10 text-sm' : 'h-6 w-6 text-xs'} p-0 font-bold rounded ${
                  stemSolos[stem]
                    ? 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30 hover:text-amber-400'
                    : 'text-zinc-600 hover:text-zinc-400'
                }`}
                onClick={() => toggleStemSolo(stem)}
                title={stemSolos[stem] ? 'Unsolo' : 'Solo'}
                aria-label={`${stem} ${stemSolos[stem] ? 'unsolo' : 'solo'}`}
              >
                S
              </Button>
              <Button
                variant="ghost"
                size="sm"
                className={`${touch ? 'h-10 w-10 text-sm' : 'h-6 w-6 text-xs'} p-0 font-bold rounded ${
                  stemMutes[stem]
                    ? 'bg-red-500/20 text-red-400 hover:bg-red-500/30 hover:text-red-400'
                    : 'text-zinc-600 hover:text-zinc-400'
                }`}
                onClick={() => toggleStemMute(stem)}
                title={stemMutes[stem] ? 'Unmute' : 'Mute'}
                aria-label={`${stem} ${stemMutes[stem] ? 'unmute' : 'mute'}`}
              >
                M
              </Button>
            </div>
            <span className="text-zinc-500 font-mono text-xs">
              {formatGain(stemGains[stem])} dB
            </span>
          </div>
          <Slider
            aria-label={`${stem} gain`}
            value={[stemGains[stem]]}
            min={-60}
            max={12}
            step={0.5}
            onValueChange={(vals) => setStemGain(stem, vals[0])}
            className={`[&_.bg-primary]:bg-zinc-100 [&_.border-primary]:border-zinc-100 ${
              touch ? '[&_[role=slider]]:h-7 [&_[role=slider]]:w-7 py-2' : ''
            }`}
          />
        </div>
      ))}
    </div>
  );
};
