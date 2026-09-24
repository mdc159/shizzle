import { PasscodeGate } from '@/components/auth/PasscodeGate';
import { useAuthGate } from '@/hooks/useAuthGate';
import { PipelinePanel } from '@/components/pipeline/PipelinePanel';
import { Toaster } from 'sonner';

/**
 * Standalone read-only pipeline dashboard (/dashboard): the same board the
 * in-player drawer shows, as its own page for a second screen or tablet.
 */
export const DashboardPage: React.FC = () => {
  const [authed, setAuthedTrue] = useAuthGate();

  return (
    <div className="relative h-full w-full overflow-hidden bg-black text-white antialiased">
      {authed ? (
        <main className="mx-auto h-full max-w-7xl overflow-y-auto p-6">
          <PipelinePanel active variant="page" />
        </main>
      ) : (
        <PasscodeGate onAuthed={setAuthedTrue} />
      )}
      <Toaster theme="dark" position="bottom-left" />
    </div>
  );
};
