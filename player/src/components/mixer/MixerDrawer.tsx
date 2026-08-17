import React from 'react';
import { useStore } from '@/stores/useStore';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription
} from '@/components/ui/sheet';
import { Button } from '@/components/ui/button';
import { RotateCcw } from 'lucide-react';
import { MixerSurface } from './MixerSurface';
import { useMixerReset } from '@/hooks/useMixerReset';

export const MixerDrawer: React.FC = () => {
  const { activeDrawer, setActiveDrawer } = useStore();
  const isOpen = activeDrawer === 'mixer';
  const handleReset = useMixerReset();

  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && setActiveDrawer('none')}>
      <SheetContent side="right" className="w-[300px] sm:w-[400px] border-l-zinc-800 bg-zinc-950/90 backdrop-blur-md text-zinc-100">
        <SheetHeader className="mb-8">
          <div className="flex items-center justify-between">
            <SheetTitle className="text-zinc-100">Mixer</SheetTitle>
            <Button aria-label="Reset mixer" variant="ghost" size="icon" onClick={handleReset} title="Reset all">
              <RotateCcw className="h-4 w-4" />
            </Button>
          </div>
          <SheetDescription className="text-zinc-500">
            Adjust stem volumes. Solo or mute individual stems.
          </SheetDescription>
        </SheetHeader>

        <MixerSurface />
      </SheetContent>
    </Sheet>
  );
};
