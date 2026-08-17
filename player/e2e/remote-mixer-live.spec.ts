import { test, expect } from '@playwright/test';

// Live end-to-end proof of the remote mixer path: two real browser contexts
// (player page + /remote page) talking through the REAL /api/remote/ws relay
// on a running control plane — no WebSocket stubs. Requires:
//   SHIZZLE_E2E_LIVE_RELAY=1
//   SHIZZLE_API_PROXY=http://localhost:<port>  (auth-off control plane)
// The vite dev proxy forwards both HTTP and WS to that instance.

test.skip(
  process.env.SHIZZLE_E2E_LIVE_RELAY !== '1',
  'live relay test needs a running control plane (set SHIZZLE_E2E_LIVE_RELAY=1)'
);

test('remote controls reach the player through the real relay', async ({ browser }) => {
  test.setTimeout(120_000);

  const playerCtx = await browser.newContext();
  const remoteCtx = await browser.newContext();
  const player = await playerCtx.newPage();
  const remote = await remoteCtx.newPage();

  // The client-side gate wants a token in localStorage; the test control
  // plane runs with auth off, so any value passes the server.
  for (const page of [player, remote]) {
    await page.addInitScript(() => localStorage.setItem('shizzle_token', 'live-e2e'));
  }

  await player.goto('/');
  await player.waitForFunction(
    () => !!(window as unknown as { __shizzle?: unknown }).__shizzle,
    undefined,
    { timeout: 15_000 }
  );

  await remote.goto('/remote');
  // A real WS handshake through the dev proxy must succeed.
  await expect(remote.getByTestId('remote-connection')).toHaveText(/Connected/, {
    timeout: 15_000,
  });

  const storeValue = <T>(expr: string) =>
    player.evaluate((e) => {
      const shizzle = (window as unknown as {
        __shizzle: { store: { getState: () => Record<string, Record<string, unknown>> } };
      }).__shizzle;
      const [slice, key] = e.split('.');
      return shizzle.store.getState()[slice][key] as T;
    }, expr);

  // Mute vocals on the remote → the player's store mutes vocals.
  await remote.getByRole('button', { name: 'vocals mute' }).click();
  await expect
    .poll(() => storeValue<boolean>('stemMutes.vocals'), { timeout: 10_000 })
    .toBe(true);

  // Drive the vocals fader by keyboard (Radix: End = max, Home = min). The
  // accessible name lives on the slider root, so target the thumb inside the
  // vocals strip.
  await remote.locator('[data-testid="stem-strip-vocals"] [role="slider"]').focus();
  await remote.keyboard.press('End');
  await expect
    .poll(() => storeValue<number>('stemGains.vocals'), { timeout: 10_000 })
    .toBe(12); // Radix: End = max (+12 dB)

  await remote.keyboard.press('Home');
  await expect
    .poll(() => storeValue<number>('stemGains.vocals'), { timeout: 10_000 })
    .toBe(-60);

  // Solo drums too, proving a second control type end to end.
  await remote.getByRole('button', { name: 'drums solo' }).click();
  await expect
    .poll(() => storeValue<boolean>('stemSolos.drums'), { timeout: 10_000 })
    .toBe(true);

  await playerCtx.close();
  await remoteCtx.close();
});
