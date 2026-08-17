import React from 'react';
import { useStore } from '@/stores/useStore';
import { Sheet, SheetContent } from '@/components/ui/sheet';
import { PipelinePanel } from './PipelinePanel';

export const PipelineDrawer: React.FC = () => {
  const { activeDrawer, setActiveDrawer } = useStore();
  const isOpen = activeDrawer === 'pipeline';

  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && setActiveDrawer('none')}>
      <SheetContent side="left" className="w-[96vw] overflow-y-auto border-r-zinc-800 bg-zinc-950 text-zinc-100 sm:max-w-[96vw] lg:max-w-6xl">
        <PipelinePanel active={isOpen} variant="drawer" />
      </SheetContent>
    </Sheet>
  );
};
