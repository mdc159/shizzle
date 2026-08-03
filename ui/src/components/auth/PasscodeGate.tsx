import React, { useState } from 'react';
import { login } from '@/lib/api';
import { Loader2 } from 'lucide-react';

interface PasscodeGateProps {
  onAuthed: () => void;
}

/**
 * Shared-passcode gate (design spec §7). Shown until a valid device token
 * exists. One passcode, entered once per device.
 */
export const PasscodeGate: React.FC<PasscodeGateProps> = ({ onAuthed }) => {
  const [passcode, setPasscode] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!passcode || busy) return;
    setBusy(true);
    setError(null);
    try {
      const { ok } = await login(passcode);
      if (ok) {
        onAuthed();
      } else {
        setError('Incorrect passcode');
        setPasscode('');
      }
    } catch {
      setError('Could not reach the server. Try again.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="absolute inset-0 flex items-center justify-center bg-zinc-950 px-6">
      <form onSubmit={submit} className="w-full max-w-sm space-y-6 text-center">
        <div className="space-y-2">
          <h1 className="text-4xl font-bold tracking-tighter text-zinc-100">Shizzle</h1>
          <p className="text-zinc-500">Enter the passcode to continue</p>
        </div>
        <input
          type="password"
          inputMode="text"
          autoFocus
          value={passcode}
          onChange={(e) => setPasscode(e.target.value)}
          placeholder="Passcode"
          autoComplete="current-password"
          className="w-full rounded-xl border border-zinc-700 bg-zinc-900 px-4 py-3 text-center text-lg text-zinc-100 outline-none focus:border-zinc-400"
        />
        {error && <p className="text-sm text-red-400">{error}</p>}
        <button
          type="submit"
          disabled={busy || !passcode}
          className="flex w-full items-center justify-center gap-2 rounded-xl bg-zinc-100 px-4 py-3 text-lg font-medium text-zinc-900 transition hover:bg-white disabled:opacity-40"
        >
          {busy && <Loader2 className="h-5 w-5 animate-spin" />}
          Enter
        </button>
      </form>
    </div>
  );
};
