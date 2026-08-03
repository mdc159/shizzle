/**
 * Device-token auth for the shared-passcode gate (design spec §7).
 *
 * The token is stored in localStorage and sent as `Authorization: Bearer` on
 * every API call. Media (/cdn/*) is gated separately by CloudFront signed
 * cookies the server sets at login — those ride along same-origin, no JS.
 */

const TOKEN_KEY = 'shizzle_token';

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

export function hasToken(): boolean {
  return !!getToken();
}

/** fetch() wrapper that adds the bearer token and sends same-origin cookies. */
export async function authFetch(input: string, init: RequestInit = {}): Promise<Response> {
  const token = getToken();
  const headers = new Headers(init.headers);
  if (token) headers.set('Authorization', `Bearer ${token}`);
  return fetch(input, { ...init, headers, credentials: 'include' });
}
