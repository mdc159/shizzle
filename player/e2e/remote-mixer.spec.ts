import { test, expect, type Page } from '@playwright/test';

// Remote mixer surface (/remote) and the player-side sync bridge, tested
// against a stubbed WebSocket so no control plane is needed: the stub records
// outbound frames (window.__wsSent) and lets the test inject inbound frames
// (window.__wsPush).

const stubWebSocket = (page: Page) =>
  page.addInitScript(() => {
    localStorage.setItem('shizzle_token', 'e2e-token');
    const instances: Array<{ onmessage?: (ev: { data: string }) => void }> = [];
    const sent: string[] = [];
    class FakeWebSocket {
      static OPEN = 1;
      static CONNECTING = 0;
      static CLOSING = 2;
      static CLOSED = 3;
      url: string;
      readyState = 0;
      onopen?: (ev: unknown) => void;
      onclose?: (ev: unknown) => void;
      onerror?: (ev: unknown) => void;
      onmessage?: (ev: { data: string }) => void;
      constructor(url: string) {
        this.url = url;
        instances.push(this);
        setTimeout(() => {
          this.readyState = FakeWebSocket.OPEN;
          this.onopen?.({});
        }, 0);
      }
      send(data: string) {
        sent.push(data);
      }
      close() {
        this.readyState = FakeWebSocket.CLOSED;
        this.onclose?.({});
      }
    }
    (window as unknown as Record<string, unknown>).WebSocket = FakeWebSocket;
    (window as unknown as Record<string, unknown>).__wsSent = sent;
    (window as unknown as Record<string, unknown>).__wsPush = (data: string) =>
      instances.forEach((ws) => ws.onmessage?.({ data }));
  });

const sentFrames = (page: Page) =>
  page.evaluate(() =>
    ((window as unknown as Record<string, unknown>).__wsSent as string[]).map(
      (f) => JSON.parse(f) as Record<string, unknown>
    )
  );

test('remote page publishes commands and applies state snapshots', async ({ page }) => {
  test.setTimeout(60_000);
  await stubWebSocket(page);
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));

  await page.goto('/remote');
  await expect(page.getByText('Shizzle Remote')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByTestId('remote-connection')).toHaveText(/Connected/, { timeout: 5_000 });

  // Touching a control publishes the matching command frame.
  await page.getByRole('button', { name: 'vocals mute' }).click();
  await expect
    .poll(async () => (await sentFrames(page)).some(
      (f) => f.type === 'mute' && f.stem === 'vocals' && f.on === true
    ), { timeout: 5_000 })
    .toBe(true);

  // An inbound state snapshot updates the surface (track title from player).
  await page.evaluate(() =>
    (window as unknown as { __wsPush: (d: string) => void }).__wsPush(
      JSON.stringify({
        type: 'state',
        track: 'Test Anthem',
        gains: { vocals: -6, drums: 0, bass: 0, guitar: 0, piano: 0, shizzle: 0 },
        mutes: { vocals: false, drums: false, bass: false, guitar: false, piano: false, shizzle: false },
        solos: { vocals: false, drums: false, bass: false, guitar: false, piano: false, shizzle: false },
        master: 1,
      })
    )
  );
  await expect(page.getByTestId('remote-track')).toHaveText('Playing: Test Anthem', { timeout: 5_000 });
});

test('player applies inbound mixer commands to the store', async ({ page }) => {
  test.setTimeout(60_000);
  await stubWebSocket(page);
  await page.route('**/api/**', (route) => route.fulfill({ json: {} }));

  await page.goto('/');

  // The main screen links to both companion pages in a new tab.
  const remoteLink = page.getByRole('link', { name: /remote mixer/i });
  await expect(remoteLink).toHaveAttribute('href', '/remote', { timeout: 10_000 });
  await expect(remoteLink).toHaveAttribute('target', '_blank');
  const dashboardLink = page.getByRole('link', { name: /pipeline dashboard/i });
  await expect(dashboardLink).toHaveAttribute('href', '/dashboard');
  await expect(dashboardLink).toHaveAttribute('target', '_blank');
  // Dev builds expose the real store for probes.
  await page.waitForFunction(() => !!(window as unknown as { __shizzle?: unknown }).__shizzle, undefined, { timeout: 10_000 });

  await page.evaluate(() =>
    (window as unknown as { __wsPush: (d: string) => void }).__wsPush(
      JSON.stringify({ type: 'mix', stem: 'vocals', gainDb: -24 })
    )
  );
  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const shizzle = (window as unknown as {
            __shizzle: { store: { getState: () => { stemGains: Record<string, number> } } };
          }).__shizzle;
          return shizzle.store.getState().stemGains.vocals;
        }),
      { timeout: 5_000 }
    )
    .toBe(-24);

  await page.evaluate(() =>
    (window as unknown as { __wsPush: (d: string) => void }).__wsPush(
      JSON.stringify({ type: 'mute', stem: 'drums', on: true })
    )
  );
  await expect
    .poll(
      () =>
        page.evaluate(() => {
          const shizzle = (window as unknown as {
            __shizzle: { store: { getState: () => { stemMutes: Record<string, boolean> } } };
          }).__shizzle;
          return shizzle.store.getState().stemMutes.drums;
        }),
      { timeout: 5_000 }
    )
    .toBe(true);
});
