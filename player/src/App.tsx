import { useEffect, useState } from 'react';
import { PlayerShell } from '@/components/player/PlayerShell';
import { LibraryDrawer } from '@/components/library/LibraryDrawer';
import { MixerDrawer } from '@/components/mixer/MixerDrawer';
import { AddSourceModal } from '@/components/source/AddSourceModal';
import { PasscodeGate } from '@/components/auth/PasscodeGate';
import { Toaster } from 'sonner';
import { useStore } from '@/stores/useStore';
import { hasToken } from '@/lib/auth';
import { refreshMediaSession } from '@/lib/api';
import { useRemoteSync } from '@/hooks/useRemoteSync';

/** Applies remote mixer commands and publishes mix state (mounted when authed). */
const RemoteSyncBridge = () => {
  useRemoteSync('player');
  return null;
};

function App() {
  const { setActiveDrawer, togglePlay } = useStore();
  const [authed, setAuthed] = useState<boolean>(() => hasToken());

  // Re-arm CloudFront media cookies whenever we hold a token (fresh login or a
  // returning device). Best-effort: playback surfaces its own errors if unmet.
  useEffect(() => {
    if (!authed) return;
    refreshMediaSession().catch(() => {
      /* CDN may be unwired (front end still viewable); ignore */
    });
  }, [authed]);

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

  if (!authed) {
    return (
      <div className="relative w-full h-full bg-black text-white antialiased">
        <PasscodeGate onAuthed={() => setAuthed(true)} />
        <Toaster theme="dark" position="bottom-left" />
      </div>
    );
  }

  return (
    <div className="relative w-full h-full bg-black text-white antialiased">
      <PlayerShell />
      <RemoteSyncBridge />

      {/* Overlays */}
      <LibraryDrawer />
      <MixerDrawer />
      <AddSourceModal />

      <Toaster theme="dark" position="bottom-left" />
    </div>
  );
}

export default App;
