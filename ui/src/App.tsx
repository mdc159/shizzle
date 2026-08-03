import { useEffect } from 'react';
import { PlayerShell } from '@/components/player/PlayerShell';
import { LibraryDrawer } from '@/components/library/LibraryDrawer';
import { MixerDrawer } from '@/components/mixer/MixerDrawer';
import { AddSourceModal } from '@/components/source/AddSourceModal';
import { Toaster } from 'sonner';
import { useStore } from '@/stores/useStore';

function App() {
  const { setActiveDrawer, togglePlay } = useStore();

  // Global Keyboard Shortcuts
  useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      // Ignore if typing in an input
      if (e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement) return;

      switch (e.key.toLowerCase()) {
        case ' ':
          e.preventDefault();
          togglePlay();
          break;
        case 'l':
          setActiveDrawer('library');
          break;
        case 'm':
          setActiveDrawer('mixer');
          break;
        case '/':
          e.preventDefault(); // prevent quick find
          setActiveDrawer('source');
          break;
        case 'escape':
          setActiveDrawer('none');
          break;
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => window.removeEventListener('keydown', handleKeyDown);
  }, [togglePlay, setActiveDrawer]);

  return (
    <div className="relative w-full h-full bg-black text-white antialiased">
      <PlayerShell />

      {/* Overlays */}
      <LibraryDrawer />
      <MixerDrawer />
      <AddSourceModal />

      <Toaster theme="dark" position="bottom-left" />
    </div>
  );
}

export default App;
