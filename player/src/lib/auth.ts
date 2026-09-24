/**
 * Device-token auth for the shared-passcode gate (design spec §7).
 *
 * The token is stored in localStorage and sent as `Authorization: Bearer` on
 * every API call. Media (/cdn/*) is gated separately by CloudFront signed
 * cookies the server sets at login — those ride along same-origin, no JS.
 *
 * Recovery (issue #30): production intentionally runs with an empty
 * SHIZZLE_PASSCODE — any non-empty passcode is accepted while the
 * gate is off — and users must never see a passcode prompt for that case. A
 * stored token can still go bad (7-day TTL, or an AUTH_VERSION bump /
 * passcode rotation revoking every token at once, invariant E4). `authFetch`
 * treats a 401 as session invalidation: try one silent `POST /api/auth`
 * with a fixed placeholder passcode (the schema requires at least one
 * character, and any value is accepted while the gate is off), and retry
 * the original request once with the fresh token. Only when the server itself rejects that silent
 * attempt (a real passcode is configured) do we notify subscribers so the
 * UI falls back to PasscodeGate.
 */

const TOKEN_KEY = 'shizzle_token';

/**
 * Passcode sent by silent re-authentication. `AuthRequest.passcode` requires
 * min_length=1, so an empty string is a 422. With the gate off any value is
 * accepted; with a real passcode configured this is simply rejected (401).
 */
export const SILENT_REAUTH_PASSCODE = 'silent-reauth';

/** Upper bound on one silent re-auth request, so a hung POST cannot wedge
 * every later 401 behind a single-flight promise that never settles. */
const SILENT_REAUTH_TIMEOUT_MS = 10_000;

export function getToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

export function setToken(token: string): void {
  try {
    localStorage.setItem(TOKEN_KEY, token);
  } catch {
    /* private mode / storage disabled — token lives for this page load only */
  }
}

export function clearToken(): void {
  try {
    localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private mode / storage disabled — nothing to clear */
  }
}

export function hasToken(): boolean {
  return !!getToken();
}

type AuthInvalidatedListener = () => void;
const invalidatedListeners = new Set<AuthInvalidatedListener>();

/**
 * Subscribe to forced sign-outs: fires when a silent re-auth attempt is
 * itself rejected by the server (a real passcode is configured), so every
 * route (app, dashboard, remote) can drop back to PasscodeGate together,
 * even mid-session. Returns an unsubscribe function.
 */
export function subscribeAuthInvalidated(cb: AuthInvalidatedListener): () => void {
  invalidatedListeners.add(cb);
  return () => invalidatedListeners.delete(cb);
}

function notifyAuthInvalidated(): void {
  invalidatedListeners.forEach((cb) => cb());
}

/** Single-attempt fetch with the bearer token attached — no 401 handling. */
function rawAuthedFetch(input: string, init: RequestInit): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return fetch(input, { ...init, headers, credentials: 'include' });
}

type SilentReauthResult = 'ok' | 'rejected' | 'error';

let reauthPromise: Promise<SilentReauthResult> | null = null;

/**
 * One silent re-authentication attempt (`POST /api/auth` with
 * `SILENT_REAUTH_PASSCODE`).
 * Concurrent callers (multiple 401s racing, a WS 4401 close) share the same
 * in-flight attempt so we never issue parallel or repeated re-auth calls.
 */
export function silentReauth(): Promise<SilentReauthResult> {
  if (!reauthPromise) {
    reauthPromise = performSilentReauth().finally(() => {
      reauthPromise = null;
    });
  }
  return reauthPromise;
}

async function performSilentReauth(): Promise<SilentReauthResult> {
  let response: Response;
  try {
    response = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      credentials: 'include',
      body: JSON.stringify({ passcode: SILENT_REAUTH_PASSCODE }),
      signal: AbortSignal.timeout(SILENT_REAUTH_TIMEOUT_MS),
    });
  } catch {
    return 'error'; // network failure or timeout — not a rejection
  }

  if (response.status === 401 || response.status === 422) {
    // A real passcode is configured (401), or the request was refused as
    // invalid (422): either way silent recovery cannot succeed.
    // Clear here too (not just in authFetch's caller): a WS 4401 close
    // drives this same silent attempt directly, with no authFetch 401 of
    // its own to have cleared the token first.
    clearToken();
    notifyAuthInvalidated();
    return 'rejected';
  }
  if (!response.ok) return 'error';

  let data: { token?: string };
  try {
    data = await response.json();
  } catch {
    return 'error';
  }
  if (!data.token) return 'error';

  setToken(data.token);

  // Re-arm CloudFront media cookies under the freshly recovered token
  // (mirrors the mount-time refresh App.tsx does for a returning device).
  // One attempt, no 401 retry here: this call already runs inside the
  // single-flight re-auth, so routing it through authFetch's own 401
  // handling would await this very promise and deadlock.
  try {
    await rawAuthedFetch('/api/media/session', {
      method: 'POST',
      signal: AbortSignal.timeout(SILENT_REAUTH_TIMEOUT_MS),
    });
  } catch {
    /* CDN may be unwired (front end still viewable); ignore */
  }

  return 'ok';
}

/**
 * fetch() wrapper that adds the bearer token and sends same-origin cookies.
 *
 * A 401 means the stored token was rejected. Recover silently once via
 * `silentReauth`, retry the original request with the fresh token, and
 * surface the original 401 only if recovery itself is rejected or fails.
 * Non-401 failures (network errors, 5xx) are returned/thrown unchanged and
 * never touch the stored token.
 */
export async function authFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const sentToken = getToken();
  const response = await rawAuthedFetch(input, init);
  if (response.status !== 401) return response;

  // A concurrent request may already have recovered while this one was in
  // flight with the old token: retry with the newer token rather than
  // clearing it and re-authenticating again.
  const currentToken = getToken();
  if (currentToken && currentToken !== sentToken) {
    return rawAuthedFetch(input, init);
  }

  // Keep the rejected token until re-auth resolves: a transient failure must
  // not leave storage empty while the UI still reports authenticated. A
  // real rejection clears it inside performSilentReauth.
  const result = await silentReauth();
  if (result === 'ok') {
    return rawAuthedFetch(input, init);
  }
  return response;
}
