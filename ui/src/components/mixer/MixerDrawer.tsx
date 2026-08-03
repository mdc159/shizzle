import React from 'react';
import { useStore } from '@/stores/useStore';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription
} from '@/components/ui/sheet';
import { Slider } from '@/components/ui/slider';
import { Button } from '@/components/ui/button';
import { RotateCcw } from 'lucide-react';
import { audioManager, StemAudioManager } from '@/lib/audio';
import type { StemId } from '@/types/karaoke';

export const MixerDrawer: React.FC = () => {
  const {
    activeDrawer, setActiveDrawer,
    stemGains, setStemGain,
    stemMutes, toggleStemMute,
    stemSolos, toggleStemSolo,
  } = useStore();
  const isOpen = activeDrawer === 'mixer';

  const stems = Object.keys(stemGains) as StemId[];

  // Update both store and audio engine
  const handleGainChange = (stem: StemId, dbValue: number) => {
    setStemGain(stem, dbValue);
    const linearGain = StemAudioManager.dbToLinear(dbValue);
    audioManager.setGain(stem, linearGain);
  };

  const handleReset = () => {
    stems.forEach(stem => {
      setStemGain(stem, 0);
      if (stemMutes[stem]) toggleStemMute(stem);
      if (stemSolos[stem]) toggleStemSolo(stem);
      audioManager.setGain(stem, 1.0);
    });
  };

  const formatGain = (db: number) => {
    if (db <= -60) return '-∞';
    return `${db >= 0 ? '+' : ''}${db.toFixed(1)}`;
  };

  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && setActiveDrawer('none')}>
      <SheetContent side="right" className="w-[300px] sm:w-[400px] border-l-zinc-800 bg-zinc-950/90 backdrop-blur-md text-zinc-100">
        <SheetHeader className="mb-8">
          <div className="flex items-center justify-between">
            <SheetTitle className="text-zinc-100">Mixer</SheetTitle>
            <Button variant="ghost" size="icon" onClick={handleReset} title="Reset all">
              <RotateCcw className="h-4 w-4" />
            </Button>
          </div>
          <SheetDescription className="text-zinc-500">
            Adjust stem volumes. Solo or mute individual stems.
          </SheetDescription>
        </SheetHeader>

        <div className="space-y-6">
          {stems.map(stem => (
            <div key={stem} className="space-y-2">
              <div className="flex items-center justify-between text-sm">
                <div className="flex items-center gap-2">
                  <span className="capitalize font-medium text-zinc-300">{stem}</span>
                  <Button
                    variant="ghost"
                    size="sm"
                    className={`h-6 w-6 p-0 text-xs font-bold rounded ${
                      stemSolos[stem]
                        ? 'bg-amber-500/20 text-amber-400 hover:bg-amber-500/30 hover:text-amber-400'
                        : 'text-zinc-600 hover:text-zinc-400'
                    }`}
                    onClick={() => toggleStemSolo(stem)}
                    title={stemSolos[stem] ? 'Unsolo' : 'Solo'}
                  >
                    S
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    className={`h-6 w-6 p-0 text-xs font-bold rounded ${
                      stemMutes[stem]
                        ? 'bg-red-500/20 text-red-400 hover:bg-red-500/30 hover:text-red-400'
                        : 'text-zinc-600 hover:text-zinc-400'
                    }`}
                    onClick={() => toggleStemMute(stem)}
                    title={stemMutes[stem] ? 'Unmute' : 'Mute'}
                  >
                    M
                  </Button>
                </div>
                <span className="text-zinc-500 font-mono text-xs">
                  {formatGain(stemGains[stem])} dB
                </span>
              </div>
              <Slider
                value={[stemGains[stem]]}
                min={-60}
                max={12}
                step={0.5}
                onValueChange={(vals) => handleGainChange(stem, vals[0])}
                className="[&_.bg-primary]:bg-zinc-100 [&_.border-primary]:border-zinc-100"
              />
            </div>
          ))}
        </div>
      </SheetContent>
    </Sheet>
  );
};
