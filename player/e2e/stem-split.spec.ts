import { test, expect } from '@playwright/test';
import path from 'path';

const MP4_PATH = path.resolve('X:/01 Music/04K Video Downloader/Mr. Brownstone.mp4');

// Full pipeline: upload, split, play entire track to completion
test('upload MP4, split stems, play full track', async ({ page }) => {
  await page.goto('/');

  // Upload flow
  await page.keyboard.press('/');
  await expect(page.getByText('Add Source')).toBeVisible();

  const fileInput = page.locator('input[type="file"]');
  await fileInput.setInputFiles(MP4_PATH);
  await expect(page.getByText('Mr. Brownstone.mp4')).toBeVisible();

  await page.getByRole('button', { name: /Split Stems/i }).click();

  // Wait through all pipeline stages
  await expect(page.getByText('Queued')).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText('Extracting Audio')).toBeVisible({ timeout: 30_000 });
  await expect(page.getByText('Separating Stems')).toBeVisible({ timeout: 60_000 });
  await expect(page.getByText('Complete')).toBeVisible({ timeout: 10 * 60 * 1000 });
  await expect(page.getByRole('link', { name: /Download Multi-Track/i })).toBeVisible();

  // Close modal, open library, load track
  await page.getByRole('button', { name: /Done/i }).click();
  await page.keyboard.press('l');
  await page.getByTitle('Refresh library').click();
  await page.getByText('Mr. Brownstone').click();

  // Wait for video element to be ready
  const video = page.locator('video');
  await expect(video).toBeVisible({ timeout: 10_000 });
  await expect(video).toHaveAttribute('src', /video\.mp4/, { timeout: 10_000 });
  await video.evaluate((v: HTMLVideoElement) =>
    new Promise<void>((resolve) => {
      if (v.readyState >= 2) return resolve();
      v.addEventListener('loadeddata', () => resolve(), { once: true });
    })
  );

  // Get the actual duration from the video element
  const duration = await video.evaluate((v: HTMLVideoElement) => v.duration);
  expect(duration).toBeGreaterThan(60); // sanity: should be ~228s

  // Hit play
  await page.locator('button.rounded-full').click();

  // Verify playback is actually running after 2s
  await page.waitForTimeout(2000);
  const earlyTime = await video.evaluate((v: HTMLVideoElement) => v.currentTime);
  expect(earlyTime).toBeGreaterThan(0);

  // Now wait for the track to finish — poll until video ends or we time out
  // duration is ~228s, give 30s buffer
  const maxWaitMs = (duration + 30) * 1000;
  await video.evaluate(
    (v: HTMLVideoElement) =>
      new Promise<void>((resolve) => {
        if (v.ended) return resolve();
        v.addEventListener('ended', () => resolve(), { once: true });
      }),
    { timeout: maxWaitMs },
  );

  // Video reached the end
  const finalTime = await video.evaluate((v: HTMLVideoElement) => v.currentTime);
  expect(finalTime).toBeGreaterThanOrEqual(duration - 1); // within 1s of full duration
});
