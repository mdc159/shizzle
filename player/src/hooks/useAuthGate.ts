import { useEffect, useState } from 'react';
import { hasToken, subscribeAuthInvalidated } from '@/lib/auth';

/**
 * Route-level PasscodeGate state, shared by App/Dashboard/RemoteMixerPage.
 * Starts from whatever token is on disk, then stays in sync with forced
 * sign-outs: `authFetch`'s 401 handling (or a WS 4401 close) calls
 * `subscribeAuthInvalidated`'s listeners only when a silent re-auth attempt
 * is itself rejected by the server (a real passcode is configured) — a
 * merely-expired/revoked token recovers silently and never reaches here.
 */
export function useAuthGate(): [boolean, () => void] {
  const [authed, setAuthed] = useState<boolean>(() => hasToken());

  useEffect(() => {
    const unsubscribe = subscribeAuthInvalidated(() => setAuthed(false));
    // An invalidation may have fired between the initial hasToken() read and
    // this subscription; the event is not replayed, so re-check once here.
    let active = true;
    queueMicrotask(() => {
      if (active && !hasToken()) setAuthed(false);
    });
    return () => {
      active = false;
      unsubscribe();
    };
  }, []);

  return [authed, () => setAuthed(true)];
}
