import { test, expect } from '@playwright/test';

// Regression test for issue #3: the library drawer clipped everything below
// the fold and the mouse wheel did nothing, so tracks past the first screen
// were unreachable. Runs against the local dev server with a mocked control
// plane — no backend or credentials required.

const tracks = Array.from({ length: 27 }, (_, i) => ({
  id: `e2e-track-${i + 1}`,
  title: `Track ${i + 1}`,
  artist: `Artist ${i + 1}`,
  slug: `e2e-track-${i + 1}`,
  duration: 180 + i,
  publicUrl: `/tracks/e2e-track-${i + 1}`,
  status: 'ready' as const,
}));

test('library drawer scrolls to reveal all 27 tracks', async ({ page }) => {
  // The global config uses 15-minute timeouts sized for full-track playback;
  // this UI test needs seconds, not minutes.
  test.setTimeout(60_000);

  await page.addInitScript(() => {
    localStorage.setItem('shizzle_token', 'e2e-token');
  });
  await page.route('**/api/media/session', (route) =>
    route.fulfill({ json: { cloudfront: false } })
  );
  await page.route('**/api/library', (route) => route.fulfill({ json: { tracks } }));

  await page.goto('/');
  await page.keyboard.press('l');

  const first = page.getByText('Track 1', { exact: true });
  const last = page.getByText('Track 27', { exact: true });

  await expect(first).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText('27 tracks available')).toBeVisible({ timeout: 5_000 });

  // The tail of the list starts out below the fold...
  await expect(last).not.toBeInViewport({ timeout: 5_000 });

  // ...and mouse-wheel scrolling over the drawer must reach it.
  await first.hover();
  for (let i = 0; i < 10; i++) {
    await page.mouse.wheel(0, 600);
  }
  await expect(last).toBeInViewport({ timeout: 5_000 });

  // The row is genuinely interactive at the bottom of the list.
  await expect(last).toBeVisible({ timeout: 5_000 });
});
