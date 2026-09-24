import { expect, test, type Page } from '@playwright/test';

// Regression test for issue #27: the Space keyboard shortcut in App.tsx
// called the Zustand store's togglePlay() directly. That flips the UI to
// "Pause" but never invokes handlePlay on the media engine — the real
// play() path only lived behind the transport button's click handler.
// Confirmed in production: pressing Space changed the button to Pause while
// the video stayed paused at the same currentTime.
//
// The fix routes Space through getTransportControls()
// (player/src/lib/playback/transportControls.ts), the exact user-gesture
// play/pause callbacks and readiness gate (`ready`) the transport button
// itself uses (PlayerShell registers handlePlay/handlePause/mediaReady;
// TransportControls' click handler is `if (playing) onPause(); else void
// onPlay();` guarded by `disabled={!currentTrack || !ready}`).
//
// Runs fully offline against the local dev server (same harness style as
// manifest-trim.spec.ts): the control plane, manifest, six synthetic WAV
// stems, and a tiny real MP4 video are all served from page.route(). The
// video route is deliberately delayed in the "not ready" case so the test
// can press Space while PlayerShell's readiness gate (isReady &&
// bufferedVideoSrc && !isBufferingVideo) is still false.
//
// Every assertion reads the real <video> element's clock (currentTime) or
// the `ended` flag — never just the Zustand `playing` flag, which is
// exactly what lied before the fix.

const STEM_IDS = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'shizzle'] as const;
const TRACK_SLUG = 'e2e-space-shortcut';
const TRACK_DURATION_SECONDS = 3;

declare global {
  interface Window {
    __shizzle: { store: { getState(): { playing: boolean } } };
  }
}

/** Minimal valid PCM16 stereo WAV; the engine only needs canplay to fire. */
function wavBytes(seconds = 2): Buffer {
  const sampleRate = 44_100;
  const channels = 2;
  const dataBytes = seconds * sampleRate * channels * 2;
  const buffer = Buffer.alloc(44 + dataBytes);
  buffer.write('RIFF', 0, 'ascii');
  buffer.writeUInt32LE(36 + dataBytes, 4);
  buffer.write('WAVE', 8, 'ascii');
  buffer.write('fmt ', 12, 'ascii');
  buffer.writeUInt32LE(16, 16);
  buffer.writeUInt16LE(1, 20); // PCM
  buffer.writeUInt16LE(channels, 22);
  buffer.writeUInt32LE(sampleRate, 24);
  buffer.writeUInt32LE(sampleRate * channels * 2, 28);
  buffer.writeUInt16LE(channels * 2, 32);
  buffer.writeUInt16LE(16, 34);
  buffer.write('data', 36, 'ascii');
  buffer.writeUInt32LE(dataBytes, 40);
  return buffer;
}

// 3 s of black 160x90 H.264 (baseline profile), audio-less, faststart,
// generated with:
//   ffmpeg -f lavfi -i color=c=black:s=160x90:r=10:d=3 -pix_fmt yuv420p \
//     -c:v libx264 -profile:v baseline -level 3.0 -movflags +faststart -an out.mp4
// Unlike manifest-trim.spec.ts's clip (never actually played there, only
// probed for gain nodes), this one must genuinely decode and advance when
// played in headless Chromium, since this spec asserts on the real
// video.currentTime clock.
const VIDEO_MP4_BASE64 =
  'AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAOdbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAC7gAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAAAsd0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAC7gAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAKAAAABaAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAAu4AAAAAAABAAAAAAI/bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAAeABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAAB6m1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAAAapzdGJsAAAAunN0c2QAAAAAAAAAAQAAAKphdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAKAAWgBIAAAASAAAAAAAAAABFUxhdmM2Mi4yOC4xMDEgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAAMGF2Y0MBQsAe/+EAGGdCwB7ZAo35MBEAAAMAAQAAAwAUDxYuSAEABWjLg8sgAAAAEHBhc3AAAAABAAAAAQAAABRidHJ0AAAAAAAACjgAAAAAAAAAGHN0dHMAAAAAAAAAAQAAAB4AAAQAAAAAFHN0c3MAAAAAAAAAAQAAAAEAAAAcc3RzYwAAAAAAAAABAAAAAQAAAB4AAAABAAAAjHN0c3oAAAAAAAAAAAAAAB4AAAKyAAAACgAAAAsAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAUc3RjbwAAAAAAAAABAAADzQAAAGJ1ZHRhAAAAWm1ldGEAAAAAAAAAIWhkbHIAAAAAAAAAAG1kaXJhcHBsAAAAAAAAAAAAAAAALWlsc3QAAAAlqXRvbwAAAB1kYXRhAAAAAQAAAABMYXZmNjIuMTIuMTAxAAAACGZyZWUAAAPdbWRhdAAAAnEGBf//bdxF6b3m2Ui3lizYINkj7u94MjY0IC0gY29yZSAxNjUgcjMyMjMgMDQ4MGNiMCAtIEguMjY0L01QRUctNCBBVkMgY29kZWMgLSBDb3B5bGVmdCAyMDAzLTIwMjUgLSBodHRwOi8vd3d3LnZpZGVvbGFuLm9yZy94MjY0Lmh0bWwgLSBvcHRpb25zOiBjYWJhYz0wIHJlZj0zIGRlYmxvY2s9MTowOjAgYW5hbHlzZT0weDE6MHgxMTEgbWU9aGV4IHN1Ym1lPTcgcHN5PTEgcHN5X3JkPTEuMDA6MC4wMCBtaXhlZF9yZWY9MSBtZV9yYW5nZT0xNiBjaHJvbWFfbWU9MSB0cmVsbGlzPTEgOHg4ZGN0PTAgY3FtPTAgZGVhZHpvbmU9MjEsMTEgZmFzdF9wc2tpcD0xIGNocm9tYV9xcF9vZmZzZXQ9LTIgdGhyZWFkcz0zIGxvb2thaGVhZF90aHJlYWRzPTEgc2xpY2VkX3RocmVhZHM9MCBucj0wIGRlY2ltYXRlPTEgaW50ZXJsYWNlZD0wIGJsdXJheV9jb21wYXQ9MCBjb25zdHJhaW5lZF9pbnRyYT0wIGJmcmFtZXM9MCB3ZWlnaHRwPTAga2V5aW50PTI1MCBrZXlpbnRfbWluPTEwIHNjZW5lY3V0PTQwIGludHJhX3JlZnJlc2g9MCByY19sb29rYWhlYWQ9NDAgcmM9Y3JmIG1idHJlZT0xIGNyZj0yMy4wIHFjb21wPTAuNjAgcXBtaW49MCBxcG1heD02OSBxcHN0ZXA9NCBpcF9yYXRpbz0xLjQwIGFxPTE6MS4wMACAAAAAOWWIhA/yYoAAw+ycnJycnJycnJ111111111111111111111111111111111111111111111111114AAAAAZBmjgf4PYAAAAHQZpUB/g9gAAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAO8HsAAAABkGboDfB7A==';

async function routeCommon(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.setItem('shizzle_token', 'e2e-token');
  });
  await page.route('**/api/media/session', (route) =>
    route.fulfill({ json: { cloudfront: false } })
  );
  await page.route('**/api/library', (route) =>
    route.fulfill({
      json: {
        tracks: [
          {
            id: TRACK_SLUG,
            title: 'Space Shortcut Probe',
            artist: 'E2E',
            slug: TRACK_SLUG,
            duration: TRACK_DURATION_SECONDS,
            publicUrl: `/tracks/${TRACK_SLUG}`,
            status: 'ready',
          },
        ],
        total: 1,
      },
    })
  );
  await page.route(`**/api/tracks/${TRACK_SLUG}/manifest`, (route) =>
    route.fulfill({
      json: {
        track_id: TRACK_SLUG,
        generation: 1,
        title: 'Space Shortcut Probe',
        artist: 'E2E',
        duration: TRACK_DURATION_SECONDS,
        video: 'video.mp4',
        stems: STEM_IDS.map((id) => ({
          id,
          name: id,
          file: `stems/${id}.wav`,
          default_gain_db: 0,
        })),
      },
    })
  );
  await page.route(`**/tracks/${TRACK_SLUG}/stems/*.wav`, (route) =>
    route.fulfill({ body: wavBytes(TRACK_DURATION_SECONDS), contentType: 'audio/wav' })
  );
}

async function selectTrack(page: Page): Promise<void> {
  await page.goto('/');
  await page.getByRole('button', { name: 'Library' }).click();
  const heading = page.getByRole('heading', { name: 'Space Shortcut Probe', exact: true });
  await expect(heading).toBeAttached({ timeout: 10_000 });
  // Native click on the card (Radix drawer can clip cards out of viewport).
  await heading.evaluate((element) => {
    const card = element.closest<HTMLElement>('.cursor-pointer');
    if (!card) throw new Error('library card not found');
    card.click();
  });
}

async function videoTime(page: Page): Promise<number> {
  return page.locator('video').evaluate((video: HTMLVideoElement) => video.currentTime);
}

async function videoEnded(page: Page): Promise<boolean> {
  return page.locator('video').evaluate((video: HTMLVideoElement) => video.ended);
}

async function storePlaying(page: Page): Promise<boolean> {
  return page.evaluate(() => window.__shizzle.store.getState().playing);
}

/** Presses Space after moving focus off any button/interactive element, so
 *  App.tsx's key-ownership guard lets it reach the global play/pause
 *  shortcut instead of a focused control owning the key (see
 *  library-scroll.spec.ts's "space on a focused library row" coverage for
 *  the ownership side of that contract). Blurs via evaluate() rather than
 *  clicking a page element: the "Loading stems..." overlay sits on top of
 *  the video with no pointer-events-none, so a real click there silently
 *  blocks on Playwright's actionability wait until the overlay clears —
 *  which would swallow exactly the pre-ready window the readiness-gate test
 *  needs to press Space inside. */
async function pressSpace(page: Page): Promise<void> {
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.keyboard.press('Space');
}

async function waitForAdvancing(page: Page): Promise<void> {
  const first = await videoTime(page);
  await expect
    .poll(async () => videoTime(page), { timeout: 20_000, intervals: [100, 150, 200] })
    .toBeGreaterThan(first + 0.1);
}

async function expectFlat(page: Page): Promise<void> {
  const first = await videoTime(page);
  await page.waitForTimeout(400);
  const second = await videoTime(page);
  expect(second).toBeCloseTo(first, 1);
}

test.describe('Space shortcut drives the real media engine (issue #27)', () => {
  test('Space starts, pauses, and resumes a loaded track using the real video clock', async ({
    page,
  }) => {
    test.setTimeout(60_000);

    await routeCommon(page);
    await page.route(`**/tracks/${TRACK_SLUG}/video.mp4`, (route) =>
      route.fulfill({ body: Buffer.from(VIDEO_MP4_BASE64, 'base64'), contentType: 'video/mp4' })
    );

    await selectTrack(page);

    // Wait for the transport button's own readiness gate — the same gate
    // getTransportControls() now honors for the Space shortcut.
    await expect(page.getByRole('button', { name: 'Play' })).toBeEnabled({ timeout: 20_000 });
    expect(await videoTime(page)).toBe(0);
    expect(await storePlaying(page)).toBe(false);

    // Space starts playback: the button flips to Pause AND the video clock
    // actually advances (the bug: only the button/store flipped before).
    await pressSpace(page);
    await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible({ timeout: 5_000 });
    expect(await storePlaying(page)).toBe(true);
    await waitForAdvancing(page);

    // Space again pauses: the clock must stop, not just the store flag.
    await pressSpace(page);
    await expect(page.getByRole('button', { name: 'Play' })).toBeVisible({ timeout: 5_000 });
    expect(await storePlaying(page)).toBe(false);
    await expectFlat(page);

    // Space again resumes playback from where it paused.
    await pressSpace(page);
    await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible({ timeout: 5_000 });
    await waitForAdvancing(page);
  });

  test('Space replays a track after it ends', async ({ page }) => {
    test.setTimeout(60_000);

    await routeCommon(page);
    await page.route(`**/tracks/${TRACK_SLUG}/video.mp4`, (route) =>
      route.fulfill({ body: Buffer.from(VIDEO_MP4_BASE64, 'base64'), contentType: 'video/mp4' })
    );

    await selectTrack(page);
    await expect(page.getByRole('button', { name: 'Play' })).toBeEnabled({ timeout: 20_000 });

    await pressSpace(page);
    await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible({ timeout: 5_000 });

    // Let the ~2 s clip run out on its own; PlayerShell's onEnded wires to
    // handlePause, which returns the button to Play.
    await expect
      .poll(async () => videoEnded(page), { timeout: 15_000 })
      .toBe(true);
    await expect(page.getByRole('button', { name: 'Play' })).toBeVisible({ timeout: 5_000 });
    expect(await storePlaying(page)).toBe(false);

    // Space after the end must replay from the top, not stay a dead Pause
    // label like the pre-fix bug produced.
    await pressSpace(page);
    await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible({ timeout: 5_000 });
    await waitForAdvancing(page);
  });

  test('Space before the track is ready does not create a false playing state', async ({
    page,
  }) => {
    test.setTimeout(60_000);

    await routeCommon(page);
    // Delay the video response so PlayerShell's readiness gate (isReady &&
    // bufferedVideoSrc && !isBufferingVideo) stays false well after the
    // stems (and therefore isReady) resolve — the exact window the pre-fix
    // togglePlay() call ignored.
    await page.route(`**/tracks/${TRACK_SLUG}/video.mp4`, async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 3_000));
      await route.fulfill({ body: Buffer.from(VIDEO_MP4_BASE64, 'base64'), contentType: 'video/mp4' });
    });

    await selectTrack(page);

    // Confirm the transport button is still gated (not ready) before probing.
    await expect(page.getByRole('button', { name: 'Loading stems' })).toBeDisabled({
      timeout: 5_000,
    });

    await pressSpace(page);

    // Nothing should have flipped: no Pause label, no store playing=true, no
    // engine start. Give the false-positive path a moment to manifest before
    // asserting it never did.
    await page.waitForTimeout(500);
    expect(await storePlaying(page)).toBe(false);
    await expect(page.getByRole('button', { name: 'Pause' })).toHaveCount(0);
    expect(await videoTime(page)).toBe(0);

    // Once the delayed video resolves and the gate opens for real, Space
    // still works through the same path.
    await expect(page.getByRole('button', { name: 'Play' })).toBeEnabled({ timeout: 15_000 });
    await pressSpace(page);
    await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible({ timeout: 5_000 });
    await waitForAdvancing(page);
  });
});
