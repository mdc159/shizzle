import { test, expect, type Page } from '@playwright/test';

// The Add Source dialog accepts one YouTube link (docs/WORKER-NODES.md): the
// link goes to POST /api/submit-url, a worker node downloads and separates
// it, and the dialog follows the job to "ready". Runs against the local dev
// server with a mocked control plane.

const jobId = '21111111-2222-4333-8444-555555555555';
const trackId = 'aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee';
const youtubeLink = 'https://www.youtube.com/watch?v=BNh6gBqxzLc';

const libraryTrack = {
  id: 'e2e-track-1',
  title: 'Existing Track',
  artist: 'Existing Artist',
  slug: 'e2e-track-1',
  duration: 200,
  publicUrl: '/tracks/e2e-track-1',
  status: 'ready' as const,
};

test.use({ deviceScaleFactor: 2 });

async function openSourceDialog(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('shizzle_token', 'e2e-token');
  });
  await page.route('**/api/media/session', (route) =>
    route.fulfill({ json: { cloudfront: false } })
  );
  await page.route('**/api/library', (route) =>
    route.fulfill({ json: { tracks: [libraryTrack], total: 1 } })
  );
  await page.goto('/');
  await page.getByRole('button', { name: 'Library' }).click();
  await page.getByRole('button', { name: 'Add', exact: true }).click();
  await expect(page.getByRole('dialog', { name: 'Add Source' })).toBeVisible({ timeout: 10_000 });
}

test('a pasted YouTube link is submitted and followed through a worker node to ready', async ({
  page,
}, testInfo) => {
  test.setTimeout(60_000);

  // The job's life as the dialog will see it, one status per poll.
  const statuses = [
    { jobId, status: 'pending', sourceType: 'url' },
    { jobId, status: 'downloading', sourceType: 'url', executor: 'node:mac-mini', workerPhase: 'mac-mini: download: 40%' },
    { jobId, status: 'dispatched', sourceType: 'url', executor: 'node:mac-mini', workerPhase: 'mac-mini: separating' },
    { jobId, status: 'verifying', sourceType: 'url', executor: 'node:mac-mini' },
    { jobId, status: 'publishing', sourceType: 'url', executor: 'node:mac-mini' },
    { jobId, status: 'ready', sourceType: 'url', executor: 'node:mac-mini', trackId, title: 'Test Song', artist: 'Test Band' },
  ];
  let polls = 0;
  await page.route(`**/api/jobs/${jobId}`, (route) => {
    const body = statuses[Math.min(polls, statuses.length - 1)];
    polls += 1;
    return route.fulfill({ json: body });
  });

  let submitBody: Record<string, unknown> | null = null;
  await page.route('**/api/submit-url', async (route) => {
    submitBody = route.request().postDataJSON() as Record<string, unknown>;
    await route.fulfill({ json: { jobId, title: null, artist: null, maxWaitSeconds: 90_000 } });
  });

  await openSourceDialog(page);

  const link = page.getByLabel('YouTube link');
  await expect(link).toBeVisible();
  await link.fill(youtubeLink);
  await expect(page.getByText('A worker node downloads and separates the link')).toBeVisible();
  const submit = page.getByRole('button', { name: 'Fetch & Split Stems' });
  await expect(submit).toBeEnabled();
  await page.screenshot({ path: testInfo.outputPath('url-pasted.png') });

  await submit.click();
  await expect.poll(() => submitBody, { timeout: 10_000 }).not.toBeNull();
  expect(submitBody!.url).toBe(youtubeLink);
  // Nothing was typed, so the node fills title/artist from the video.
  expect(submitBody!).not.toHaveProperty('title');
  expect(submitBody!).not.toHaveProperty('artist');

  // The dialog follows the job, naming the node that holds it.
  await expect(page.getByText('Separating on mac-mini')).toBeVisible({ timeout: 20_000 });
  await expect(page.getByText('Complete')).toBeVisible({ timeout: 30_000 });
  const download = page.getByRole('link', { name: /Download Multi-Track/ });
  await expect(download).toHaveAttribute('href', `/api/tracks/${trackId}/download`);
  await page.screenshot({ path: testInfo.outputPath('url-ready.png') });
});

test('anything but one YouTube video cannot be submitted', async ({ page }) => {
  test.setTimeout(60_000);
  let submitted = false;
  await page.route('**/api/submit-url', async (route) => {
    submitted = true;
    await route.fulfill({ status: 400, json: { detail: 'Paste a link to one YouTube video' } });
  });
  await openSourceDialog(page);

  const link = page.getByLabel('YouTube link');
  await link.fill('https://vimeo.com/12345');
  await expect(page.getByText('Only a link to one YouTube video can be fetched.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Fetch & Split Stems' })).toBeDisabled();

  await link.fill('youtube.com/watch?v=BNh6gBqxzLc');
  await expect(page.getByText('Enter a full http(s) link.')).toBeVisible();

  await link.fill(youtubeLink);
  await expect(page.getByRole('button', { name: 'Fetch & Split Stems' })).toBeEnabled();
  expect(submitted).toBe(false);
});
