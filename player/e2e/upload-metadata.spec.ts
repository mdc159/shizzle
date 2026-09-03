import { test, expect, type Page } from '@playwright/test';

// The upload dialog prefills Title/Artist from the file name (same rules as
// the control plane's source-name parser) and sends both as multipart fields.
// Runs against the local dev server with a mocked control plane.

const jobId = '11111111-2222-4333-8444-555555555555';

const libraryTrack = {
  id: 'e2e-track-1',
  title: 'Existing Track',
  artist: 'Existing Artist',
  slug: 'e2e-track-1',
  duration: 200,
  publicUrl: '/tracks/e2e-track-1',
  status: 'ready' as const,
};

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
  // The modal polls job status after a successful upload.
  await page.route(`**/api/jobs/${jobId}`, (route) =>
    route.fulfill({ json: { jobId, status: 'pending' } })
  );

  await page.goto('/');
  await page.getByRole('button', { name: 'Library' }).click();
  await page.getByRole('button', { name: 'Add', exact: true }).click();
  await expect(page.getByRole('dialog')).toBeVisible({ timeout: 10_000 });
}

test('upload dialog prefills title and artist from the file name and sends them', async ({ page }) => {
  test.setTimeout(60_000);

  let uploadBody: string | null = null;
  await page.route('**/api/upload', async (route) => {
    uploadBody = route.request().postDataBuffer()?.toString('utf-8') ?? '';
    await route.fulfill({ json: { jobId } });
  });

  await openSourceDialog(page);

  await page.locator('input[type="file"]').setInputFiles({
    name: 'Van Halen - Hot For Teacher (Official Music Video).mp4',
    mimeType: 'video/mp4',
    buffer: Buffer.from('e2e fake video bytes'),
  });

  const titleInput = page.getByLabel('Title');
  const artistInput = page.getByLabel('Artist');
  await expect(titleInput).toHaveValue('Hot For Teacher');
  await expect(artistInput).toHaveValue('Van Halen');

  // Both fields stay editable; the edited values are what get uploaded.
  // Use edited values that do NOT appear in the file name, so the body
  // assertions below cannot pass off the multipart file part's filename.
  await titleInput.fill('Hot For Teacher (edited title)');
  await artistInput.fill('Van Halen (edited)');

  const submit = page.getByRole('button', { name: 'Split Stems' });
  await expect(submit).toBeEnabled();
  await submit.click();

  await expect.poll(() => uploadBody, { timeout: 10_000 }).not.toBeNull();
  expect(uploadBody!).toContain('name="title"');
  expect(uploadBody!).toContain('Hot For Teacher (edited title)');
  expect(uploadBody!).toContain('name="artist"');
  expect(uploadBody!).toContain('Van Halen (edited)');
  expect(uploadBody!).toContain('name="file"');
  expect(uploadBody!).toContain('Van Halen - Hot For Teacher (Official Music Video).mp4');
});

test('file name without an artist shows a hint and blocks submit until artist is typed', async ({ page }) => {
  test.setTimeout(60_000);

  await openSourceDialog(page);

  await page.locator('input[type="file"]').setInputFiles({
    name: 'Mr. Brownstone.mp4',
    mimeType: 'video/mp4',
    buffer: Buffer.from('e2e fake video bytes'),
  });

  const titleInput = page.getByLabel('Title');
  const artistInput = page.getByLabel('Artist');
  await expect(titleInput).toHaveValue('Mr. Brownstone');
  await expect(artistInput).toHaveValue('');

  // Quiet hint, not an error.
  await expect(page.getByText("Couldn't tell the artist from the file name")).toBeVisible();

  const submit = page.getByRole('button', { name: 'Split Stems' });
  await expect(submit).toBeDisabled();

  await artistInput.fill("Guns N' Roses");
  await expect(submit).toBeEnabled();
  // The quiet hint hides once the user has typed an artist.
  await expect(page.getByText("Couldn't tell the artist from the file name")).toBeHidden();
});
