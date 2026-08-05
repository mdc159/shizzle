import { expect, test, type Page } from '@playwright/test';

const enabled = process.env.SHIZZLE_PRODUCTION_PLAYBACK === '1';
const passcode = process.env.SHIZZLE_E2E_PASSCODE;

async function authenticate(page: Page): Promise<void> {
  await page.goto('/');
  const gate = page.getByPlaceholder('Passcode');
  if (await gate.isVisible().catch(() => false)) {
    if (!passcode) throw new Error('SHIZZLE_E2E_PASSCODE is required');
    await gate.fill(passcode);
    await page.getByRole('button', { name: 'Enter' }).click();
  }
  await expect(page.getByRole('button', { name: 'Library' })).toBeVisible({ timeout: 30_000 });
}

test('video staging rejects an over-cap object before playback', async ({ page }, testInfo) => {
  test.skip(!enabled, 'Set SHIZZLE_PRODUCTION_PLAYBACK=1 for the production acceptance run');
  await page.addInitScript(() => {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      if (url.includes('/video.mp4')) {
        return Promise.resolve(
          new Response('', {
            status: 200,
            headers: {
              'content-length': String(128 * 1024 * 1024 + 1),
              'content-type': 'video/mp4',
            },
          }),
        );
      }
      return nativeFetch(input, init);
    };
  });
  await authenticate(page);
  const track = await page.evaluate(async () => {
    const token = localStorage.getItem('shizzle_token');
    const response = await fetch('/api/library', {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      credentials: 'include',
    });
    if (!response.ok) throw new Error(`library HTTP ${response.status}`);
    return ((await response.json()) as { tracks: Array<{ title: string }> }).tracks[0];
  });
  await page.getByRole('button', { name: 'Library' }).click();
  const heading = page.getByRole('heading', { name: track.title, exact: true });
  await expect(heading).toBeAttached({ timeout: 30_000 });
  await heading.evaluate((element) => {
    const card = element.closest<HTMLElement>('.cursor-pointer');
    if (!card) throw new Error('library card not found');
    card.click();
  });

  await expect(page.getByText('Video exceeds the 128 MB browser-staging limit')).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByRole('button', { name: 'Loading stems' })).toBeDisabled();
  const state = await page.locator('video').evaluate((video) => ({
    source: video.currentSrc,
    stagedBytes: Number(video.dataset.stagedBytes ?? 0),
  }));
  expect(state).toEqual({ source: '', stagedBytes: 0 });
  await testInfo.attach('video-staging-cap-result', {
    body: Buffer.from(JSON.stringify({ status: 'passed', limitBytes: 128 * 1024 * 1024 })),
    contentType: 'application/json',
  });
});
