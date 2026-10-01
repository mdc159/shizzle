import { expect, test, type Page } from '@playwright/test';

// Regression test for the WebKit hard-seek loop found in production playback
// telemetry on 2026-10-01: on every iPad browser the audio cut in and out
// continuously, and each session recorded about ten hard seeks per second on
// every stem (533 in 54 s) while desktop Chromium on the same build recorded
// none.
//
// Cause: the 100 ms watchdog re-seeked all six stems whenever one reported
// being 40 ms or more off the video master. On WebKit a seek on a playing
// stem takes longer than one watchdog tick to land; until it lands the
// element reports the seek target as currentTime, so by the next tick the
// video had moved on, the stems looked 100 ms behind, and the watchdog seeked
// again on top of the seek that had not landed. And a seek that does land
// leaves the stem behind the master by however long it took, so re-seeking to
// the master's current time can never converge.
//
// The engine now waits for a correction to land and settle before judging
// sync again, backs off while corrections do not hold, and aims the next one
// ahead of the master by the lag the previous one left behind.
//
// Chromium lands buffered seeks in a few milliseconds, so this spec gives the
// stem elements WebKit's behavior by wrapping HTMLMediaElement's currentTime
// and seeking accessors before the app boots: a seek on a playing audio
// element is applied SEEK_LATENCY_MS late, and until then the element reports
// the target and `seeking === true`, as the media element spec requires.
//
// Runs fully offline against the local dev server (same harness style as
// manifest-trim.spec.ts and space-shortcut.spec.ts).

type StemState = { skewMs: number | null; hardSeeks: number };
type PlaybackMetrics = {
  stems: Record<string, StemState>;
  health: { status: string };
};

declare global {
  interface Window {
    __shizzlePlaybackHealth: { getMetrics(): PlaybackMetrics };
    __e2eSeekProbe: {
      /** Seeks issued on a stem whose previous seek had not landed yet. */
      stackedSeeks: number;
      /** Moves every stem behind the master without going through the engine. */
      knockStemsBehind(seconds: number): number;
    };
  }
}

const STEM_IDS = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'shizzle'] as const;
const TRACK_SLUG = 'e2e-resync-budget';
const TRACK_DURATION_SECONDS = 14;
/** Longer than one 100 ms watchdog tick and than the 40 ms hard threshold. */
const SEEK_LATENCY_MS = 150;
const OBSERVE_MS = 5000;
/** Expected is one per stem, aimed ahead by the lag measured during startup.
 *  The pre-fix engine issued about fifty in the same window. */
const HARD_SEEK_BUDGET = 3;
/** The production playback contract's settled-offset ceiling. */
const SETTLED_OFFSET_MS = 50;

/** Minimal valid PCM16 stereo WAV; the engine only needs canplay to fire. */
function wavBytes(seconds: number): Buffer {
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

// 14 s of black 160x90 H.264 (baseline profile), audio-less, faststart,
// generated with:
//   ffmpeg -f lavfi -i color=c=black:s=160x90:r=10:d=14 -pix_fmt yuv420p \
//     -c:v libx264 -profile:v baseline -level 3.0 -movflags +faststart -an out.mp4
const VIDEO_MP4_BASE64 =
  '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gD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQDvB7AAAAAZBm2A3wew=';

async function metrics(page: Page): Promise<PlaybackMetrics> {
  return page.evaluate(() => window.__shizzlePlaybackHealth.getMetrics());
}

async function videoTime(page: Page): Promise<number> {
  return page.locator('video').evaluate((video: HTMLVideoElement) => video.currentTime);
}

function maxHardSeeks(m: PlaybackMetrics): number {
  return Math.max(...Object.values(m.stems).map((stem) => stem.hardSeeks));
}

function maxOffsetMs(m: PlaybackMetrics): number {
  return Math.max(...Object.values(m.stems).map((stem) => Math.abs(stem.skewMs ?? Infinity)));
}

test('stems knocked behind the master resync within a hard-seek budget when seeks land late', async ({
  page,
}) => {
  test.setTimeout(120_000);

  const wav = wavBytes(TRACK_DURATION_SECONDS);
  await page.addInitScript((latencyMs) => {
    localStorage.setItem('shizzle_token', 'e2e-token');

    const proto = HTMLMediaElement.prototype;
    const currentTime = Object.getOwnPropertyDescriptor(proto, 'currentTime')!;
    const seeking = Object.getOwnPropertyDescriptor(proto, 'seeking')!;
    const nativePlay = proto.play;
    const pending = new Map<HTMLMediaElement, { target: number; timer: number }>();
    const stems = new Set<HTMLAudioElement>();
    const probe = {
      stackedSeeks: 0,
      knockStemsBehind(seconds: number): number {
        for (const el of stems) {
          currentTime.set!.call(el, Math.max(0, (currentTime.get!.call(el) as number) - seconds));
        }
        return stems.size;
      },
    };
    window.__e2eSeekProbe = probe;

    proto.play = function patchedPlay(this: HTMLMediaElement) {
      if (this instanceof HTMLAudioElement) stems.add(this);
      return nativePlay.call(this);
    };
    Object.defineProperty(proto, 'currentTime', {
      configurable: true,
      enumerable: currentTime.enumerable,
      get(this: HTMLMediaElement) {
        return pending.get(this)?.target ?? currentTime.get!.call(this);
      },
      set(this: HTMLMediaElement, value: number) {
        // Only seeks on a playing stem land late; the video master and the
        // paused startup alignment keep the browser's own behavior.
        if (!(this instanceof HTMLAudioElement) || this.paused) {
          currentTime.set!.call(this, value);
          return;
        }
        const prior = pending.get(this);
        if (prior) {
          probe.stackedSeeks += 1;
          window.clearTimeout(prior.timer);
        }
        const timer = window.setTimeout(() => {
          pending.delete(this);
          currentTime.set!.call(this, value);
        }, latencyMs);
        pending.set(this, { target: value, timer });
      },
    });
    Object.defineProperty(proto, 'seeking', {
      configurable: true,
      enumerable: seeking.enumerable,
      get(this: HTMLMediaElement) {
        return pending.has(this) || (seeking.get!.call(this) as boolean);
      },
    });
  }, SEEK_LATENCY_MS);

  await page.route('**/api/media/session', (route) =>
    route.fulfill({ json: { cloudfront: false } })
  );
  await page.route('**/api/library', (route) =>
    route.fulfill({
      json: {
        tracks: [
          {
            id: TRACK_SLUG,
            title: 'Resync Budget Probe',
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
        title: 'Resync Budget Probe',
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
  // Stems must be seekable, so answer Range requests the way the CDN does. A
  // plain 200 makes Chromium treat the stem as a non-seekable stream and land
  // every seek at 0.
  await page.route(`**/tracks/${TRACK_SLUG}/stems/*.wav`, (route) => {
    const range = /^bytes=(\d+)-(\d*)$/.exec(route.request().headers()['range'] ?? '');
    const start = range ? Number(range[1]) : 0;
    const end = range?.[2] ? Number(range[2]) : wav.length - 1;
    return route.fulfill({
      status: range ? 206 : 200,
      body: wav.subarray(start, end + 1),
      contentType: 'audio/wav',
      headers: {
        'Accept-Ranges': 'bytes',
        ...(range ? { 'Content-Range': `bytes ${start}-${end}/${wav.length}` } : {}),
      },
    });
  });
  await page.route(`**/tracks/${TRACK_SLUG}/video.mp4`, (route) =>
    route.fulfill({ body: Buffer.from(VIDEO_MP4_BASE64, 'base64'), contentType: 'video/mp4' })
  );

  await page.goto('/');
  await page.getByRole('button', { name: 'Library' }).click();
  const heading = page.getByRole('heading', { name: 'Resync Budget Probe', exact: true });
  await expect(heading).toBeAttached({ timeout: 10_000 });
  // Native click on the card (Radix drawer can clip cards out of viewport).
  await heading.evaluate((element) => {
    const card = element.closest<HTMLElement>('.cursor-pointer');
    if (!card) throw new Error('library card not found');
    card.click();
  });

  const play = page.getByRole('button', { name: 'Play' });
  await expect(play).toBeEnabled({ timeout: 20_000 });
  await play.click();
  await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible({ timeout: 5_000 });

  // Settled, healthy playback first, so everything after the knock is the
  // engine's response to it and nothing else. Startup itself may take a few
  // corrections here: the first seek refetches through the route handler and
  // lands much later than the ones after it.
  await expect
    .poll(async () => (await metrics(page)).health.status, { timeout: 10_000, intervals: [100] })
    .toBe('healthy');
  const before = await metrics(page);
  expect(Object.keys(before.stems)).toHaveLength(6);
  const videoBefore = await videoTime(page);

  // Put the ensemble where a late-landing WebKit start leaves it: together,
  // and well behind the master.
  expect(await page.evaluate(() => window.__e2eSeekProbe.knockStemsBehind(0.3))).toBe(6);

  await page.waitForTimeout(OBSERVE_MS);
  const after = await metrics(page);

  // The master kept running for the whole window: the budget below was not
  // met by playback simply having stopped.
  expect((await videoTime(page)) - videoBefore).toBeGreaterThan((OBSERVE_MS / 1000) * 0.8);
  // The loop: about ten seeks per second per stem before the fix.
  expect(maxHardSeeks(after) - maxHardSeeks(before)).toBeLessThanOrEqual(HARD_SEEK_BUDGET);
  // A correction is never issued on top of one that has not landed.
  expect(await page.evaluate(() => window.__e2eSeekProbe.stackedSeeks)).toBe(0);
  // And the budget is not met by giving up: the stems are back on the master.
  expect(maxHardSeeks(after) - maxHardSeeks(before)).toBeGreaterThanOrEqual(1);
  expect(maxOffsetMs(after)).toBeLessThanOrEqual(SETTLED_OFFSET_MS);
  expect(after.health.status).toBe('healthy');
});
