import { test, expect, type Page, type Route } from '@playwright/test';

// Regression tests for issue #30: any nonempty localStorage token was treated
// as authenticated forever, a 401 was ignored, and authFetch never cleared
// the token — so an expired (7-day TTL) or revoked (AUTH_VERSION bump /
// passcode rotation, invariant E4) token locked the user out with repeated
// "Unauthorized" errors, even across reloads.
//
// Production intentionally runs with an empty SHIZZLE_PASSCODE (any
// passcode, including empty, is accepted while the gate is off), so recovery
// must be silent: on a 401, authFetch clears the stored token, tries one
// `POST /api/auth` with passcode: '', and retries the original request with
// the fresh token. Only when that silent attempt is itself rejected (a real
// passcode is configured) does the UI fall back to PasscodeGate.
//
// All tests run fully offline via page.route() mocks against the local dev
// server, matching the style of library-scroll.spec.ts and remote-mixer.spec.ts.

const TRACKS = [
  {
    id: 'e2e-recovered-track',
    title: 'Recovered Track',
    artist: 'Comeback Artist',
    slug: 'e2e-recovered-track',
    duration: 200,
    publicUrl: '/tracks/e2e-recovered-track',
    status: 'ready' as const,
  },
];

/** Fulfil every other /api/** call with an empty 200 so unrelated background
 * polling (jobs, health) never surfaces noise in these auth-focused tests. */
const stubBackgroundApi = (page: Page) =>
  page.route('**/api/**', (route) => route.fulfill({ json: {} }));

/** `POST /api/auth` mock: accepts passcode `""` (the production posture —
 * the gate is off) and mints `freshToken`; rejects everything else with 401,
 * as the server does whenever a real SHIZZLE_PASSCODE is configured. */
const routeAuth = (
  page: Page,
  { freshToken, acceptPasscode }: { freshToken: string; acceptPasscode: string | null }
) =>
  page.route('**/api/auth', async (route: Route) => {
    const body = route.request().postDataJSON() as { passcode: string };
    if (acceptPasscode !== null && body.passcode === acceptPasscode) {
      return route.fulfill({
        json: { token: freshToken, expiresIn: 999_999, mediaCookies: false },
      });
    }
    return route.fulfill({ status: 401, json: { detail: 'Incorrect passcode' } });
  });

/** `POST /api/media/session` mock: 401 unless the bearer token is one of the
 * accepted tokens (mirrors the server's real 401-on-bad-token behaviour). */
const routeMediaSession = (page: Page, acceptedTokens: string[]) =>
  page.route('**/api/media/session', (route: Route) => {
    const auth = route.request().headers()['authorization'] ?? '';
    const token = auth.replace(/^Bearer\s+/i, '');
    if (acceptedTokens.includes(token)) {
      return route.fulfill({ json: { cloudfront: false, expiresIn: 999_999 } });
    }
    return route.fulfill({ status: 401, json: { detail: 'Authentication required' } });
  });

/** `GET /api/library` mock: 200 with `TRACKS` for accepted tokens, else the
 * given non-auth status (default 401) so the recovery path is exercised. */
const routeLibrary = (
  page: Page,
  { acceptedTokens, otherwiseStatus = 401 }: { acceptedTokens: string[]; otherwiseStatus?: number }
) =>
  page.route('**/api/library', (route: Route) => {
    const auth = route.request().headers()['authorization'] ?? '';
    const token = auth.replace(/^Bearer\s+/i, '');
    if (acceptedTokens.includes(token)) {
      return route.fulfill({ json: { tracks: TRACKS, total: TRACKS.length } });
    }
    return route.fulfill({ status: otherwiseStatus, json: { detail: 'nope' } });
  });

const setToken = (page: Page, token: string) =>
  page.addInitScript((t) => localStorage.setItem('shizzle_token', t), token);

const storedToken = (page: Page) => page.evaluate(() => localStorage.getItem('shizzle_token'));

const passcodeGateVisible = (page: Page) =>
  page.getByText('Enter the passcode to continue').isVisible().catch(() => false);

test.describe('auth recovery (app route)', () => {
  test('expired token recovers silently, library loads, no passcode UI, and reload after recovery works', async ({
    page,
  }) => {
    test.setTimeout(60_000);
    await setToken(page, 'expired-token');
    await stubBackgroundApi(page);
    await routeAuth(page, { freshToken: 'fresh-token', acceptPasscode: '' });
    await routeMediaSession(page, ['fresh-token']);
    await routeLibrary(page, { acceptedTokens: ['fresh-token'] });

    let authCalls = 0;
    page.on('request', (req) => {
      if (req.url().includes('/api/auth') && req.method() === 'POST') authCalls++;
    });

    await page.goto('/');
    // PasscodeGate must never appear for an expired token when the server
    // posture allows silent recovery.
    expect(await passcodeGateVisible(page)).toBe(false);

    await page.getByRole('button', { name: 'Library' }).click();
    await expect(page.getByText('Recovered Track')).toBeVisible({ timeout: 10_000 });
    expect(await passcodeGateVisible(page)).toBe(false);

    // The recovered token is what's now on disk.
    await expect.poll(() => storedToken(page)).toBe('fresh-token');
    // authFetch dedupes concurrent 401s (mount-time refreshMediaSession and
    // the library fetch can both 401) into a single re-auth call.
    expect(authCalls).toBeLessThanOrEqual(1);

    // Reload: the fresh token is accepted straight away, no gate, no retry.
    await page.reload();
    expect(await passcodeGateVisible(page)).toBe(false);
    await page.getByRole('button', { name: 'Library' }).click();
    await expect(page.getByText('Recovered Track')).toBeVisible({ timeout: 10_000 });
  });

  test('token revoked while the page is open recovers on the next request', async ({ page }) => {
    test.setTimeout(60_000);
    await setToken(page, 'good-token');
    await stubBackgroundApi(page);
    await routeAuth(page, { freshToken: 'rotated-token', acceptPasscode: '' });
    await routeMediaSession(page, ['good-token', 'rotated-token']);

    let revoked = false;
    await page.route('**/api/library', (route: Route) => {
      const auth = route.request().headers()['authorization'] ?? '';
      const token = auth.replace(/^Bearer\s+/i, '');
      if (!revoked && token === 'good-token') {
        return route.fulfill({ json: { tracks: TRACKS, total: TRACKS.length } });
      }
      if (token === 'rotated-token') {
        return route.fulfill({
          json: { tracks: [{ ...TRACKS[0], title: 'Post-Rotation Track' }], total: 1 },
        });
      }
      return route.fulfill({ status: 401, json: { detail: 'nope' } });
    });

    await page.goto('/');
    await page.getByRole('button', { name: 'Library' }).click();
    await expect(page.getByText('Recovered Track')).toBeVisible({ timeout: 10_000 });

    // Passcode rotation / AUTH_VERSION bump revokes the live token (E4).
    revoked = true;
    await page.locator('button[title="Refresh library"]').click();

    await expect(page.getByText('Post-Rotation Track')).toBeVisible({ timeout: 10_000 });
    expect(await passcodeGateVisible(page)).toBe(false);
    await expect.poll(() => storedToken(page)).toBe('rotated-token');
  });

  test('silent re-auth rejected shows PasscodeGate; correct passcode recovers', async ({ page }) => {
    test.setTimeout(60_000);
    await setToken(page, 'expired-token');
    await stubBackgroundApi(page);
    // A real passcode is configured server-side: the silent empty-passcode
    // attempt is rejected, but the actual passcode works.
    await routeAuth(page, { freshToken: 'manual-token', acceptPasscode: 'letmein' });
    await routeMediaSession(page, ['manual-token']);
    await routeLibrary(page, { acceptedTokens: ['manual-token'] });

    await page.goto('/');
    await expect(page.getByText('Enter the passcode to continue')).toBeVisible({ timeout: 10_000 });
    await expect.poll(() => storedToken(page)).toBe(null);

    await page.getByPlaceholder('Passcode').fill('letmein');
    await page.getByRole('button', { name: 'Enter' }).click();

    await expect(page.getByText('Enter the passcode to continue')).not.toBeVisible({ timeout: 10_000 });
    await page.getByRole('button', { name: 'Library' }).click();
    await expect(page.getByText('Recovered Track')).toBeVisible({ timeout: 10_000 });
    await expect.poll(() => storedToken(page)).toBe('manual-token');
  });

  test('a 500 on /api/library does not clear the token or log the user out', async ({ page }) => {
    test.setTimeout(60_000);
    await setToken(page, 'good-token');
    await stubBackgroundApi(page);
    await page.route('**/api/media/session', (route: Route) =>
      route.fulfill({ json: { cloudfront: false, expiresIn: 999_999 } })
    );
    await page.route('**/api/library', (route: Route) =>
      route.fulfill({ status: 500, json: { detail: 'boom' } })
    );

    await page.goto('/');
    expect(await passcodeGateVisible(page)).toBe(false);

    await page.getByRole('button', { name: 'Library' }).click();
    // The request fails, but it must not be treated as an auth rejection.
    // (Both the toast and the drawer panel show the message — assert the
    // panel's copy specifically.)
    await expect(
      page.locator('p.text-red-400', { hasText: 'Failed to fetch library' })
    ).toBeVisible({ timeout: 10_000 });
    expect(await passcodeGateVisible(page)).toBe(false);
    await expect.poll(() => storedToken(page)).toBe('good-token');
  });

  test('a network error on /api/library does not clear the token or log the user out', async ({
    page,
  }) => {
    test.setTimeout(60_000);
    await setToken(page, 'good-token');
    await stubBackgroundApi(page);
    await page.route('**/api/media/session', (route: Route) =>
      route.fulfill({ json: { cloudfront: false, expiresIn: 999_999 } })
    );
    await page.route('**/api/library', (route: Route) => route.abort('failed'));

    await page.goto('/');
    await page.getByRole('button', { name: 'Library' }).click();
    await page.waitForTimeout(1000);
    expect(await passcodeGateVisible(page)).toBe(false);
    await expect.poll(() => storedToken(page)).toBe('good-token');
  });
});

test.describe('auth recovery (dashboard route)', () => {
  test('expired token recovers silently on /dashboard, panel loads, no passcode UI', async ({
    page,
  }) => {
    test.setTimeout(60_000);
    await setToken(page, 'expired-token');
    // Register the catch-all first: Playwright resolves overlapping routes
    // most-recently-registered-first, so the specific overrides below must
    // come after it to actually take priority.
    await stubBackgroundApi(page);
    await routeAuth(page, { freshToken: 'fresh-token', acceptPasscode: '' });
    await routeMediaSession(page, ['fresh-token']);
    await page.route('**/api/jobs', (route: Route) => {
      const auth = route.request().headers()['authorization'] ?? '';
      if (auth === 'Bearer fresh-token') return route.fulfill({ json: { jobs: [] } });
      return route.fulfill({ status: 401, json: { detail: 'nope' } });
    });
    await page.route('**/api/health', (route: Route) =>
      route.fulfill({ json: { orchestratorAlive: true } })
    );

    await page.goto('/dashboard');
    expect(await passcodeGateVisible(page)).toBe(false);
    await expect(page.getByText(/orchestrator alive/i)).toBeVisible({ timeout: 10_000 });
    await expect.poll(() => storedToken(page)).toBe('fresh-token');
  });
});

test.describe('auth recovery (remote route)', () => {
  /** Fake WebSocket that lets the test force a close with a given code,
   * mirroring the server's 4401-on-bad-cookie close (remote.py). */
  const stubWebSocket = (page: Page) =>
    page.addInitScript(() => {
      const instances: Array<{
        close: (code?: number) => void;
        readyState: number;
        onopen?: (ev: unknown) => void;
        onclose?: (ev: { code: number }) => void;
      }> = [];
      class FakeWebSocket {
        static OPEN = 1;
        static CONNECTING = 0;
        static CLOSING = 2;
        static CLOSED = 3;
        readyState = 0;
        onopen?: (ev: unknown) => void;
        onclose?: (ev: { code: number }) => void;
        onerror?: (ev: unknown) => void;
        onmessage?: (ev: { data: string }) => void;
        constructor() {
          instances.push(this as unknown as (typeof instances)[number]);
          setTimeout(() => {
            this.readyState = FakeWebSocket.OPEN;
            this.onopen?.({});
          }, 0);
        }
        send() {
          /* no-op — these tests only exercise the auth/reconnect path */
        }
        close(code?: number) {
          this.readyState = FakeWebSocket.CLOSED;
          this.onclose?.({ code: code ?? 1000 });
        }
      }
      (window as unknown as Record<string, unknown>).WebSocket = FakeWebSocket;
      (window as unknown as Record<string, unknown>).__wsForceClose = (code: number) =>
        instances[instances.length - 1]?.close(code);
      (window as unknown as Record<string, unknown>).__wsCount = () => instances.length;
    });

  const forceWsClose = (page: Page, code: number) =>
    page.evaluate((c) => (window as unknown as { __wsForceClose: (code: number) => void }).__wsForceClose(c), code);

  test('a WS 4401 close triggers silent re-auth and the socket reconnects', async ({ page }) => {
    test.setTimeout(60_000);
    await setToken(page, 'expired-token');
    await stubWebSocket(page);
    // Catch-all first: overlapping routes resolve most-recently-registered
    // first, so the specific /api/auth override below must come after it.
    await stubBackgroundApi(page);
    let authCalls = 0;
    await page.route('**/api/auth', async (route: Route) => {
      authCalls++;
      return route.fulfill({ json: { token: 'fresh-token', expiresIn: 999_999, mediaCookies: false } });
    });

    await page.goto('/remote');
    await expect(page.getByTestId('remote-connection')).toHaveText(/Connected/, { timeout: 10_000 });
    expect(await passcodeGateVisible(page)).toBe(false);

    await forceWsClose(page, 4401);
    await expect(page.getByTestId('remote-connection')).toHaveText(/Reconnecting/, { timeout: 5_000 });
    await expect(page.getByTestId('remote-connection')).toHaveText(/Connected/, { timeout: 15_000 });

    expect(authCalls).toBeGreaterThanOrEqual(1);
    expect(await passcodeGateVisible(page)).toBe(false);
    await expect.poll(() => storedToken(page)).toBe('fresh-token');
  });

  test('a WS 4401 close whose silent re-auth is rejected shows PasscodeGate', async ({ page }) => {
    test.setTimeout(60_000);
    await setToken(page, 'expired-token');
    await stubWebSocket(page);
    await stubBackgroundApi(page);
    await page.route('**/api/auth', (route: Route) =>
      route.fulfill({ status: 401, json: { detail: 'Incorrect passcode' } })
    );

    await page.goto('/remote');
    await expect(page.getByTestId('remote-connection')).toHaveText(/Connected/, { timeout: 10_000 });

    await forceWsClose(page, 4401);
    await expect(page.getByText('Enter the passcode to continue')).toBeVisible({ timeout: 10_000 });
    await expect.poll(() => storedToken(page)).toBe(null);
  });
});
