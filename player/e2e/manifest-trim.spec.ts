import { expect, test, type Page } from '@playwright/test';

// Regression test for issue #25: the player applied each stem's manifest
// `default_gain_db` at load, then useAudioSync's store-sync effect overwrote
// the same single gain slot with the (0 dB) user faders, so the common
// attenuation the publisher computed for the -1.0 dBTP ceiling (invariant D3)
// never reached the rendered mix. The engine now keeps the manifest trim and
// the user fader as separate terms and renders dbToLinear(trimDb + gainDb).
//
// Runs fully offline against the local dev server (same harness style as
// library-scroll.spec.ts): the control plane, manifest, six synthetic WAV
// stems, and a tiny real MP4 video are all served from page.route().
//
// The actual rendered GainNodes are captured by wrapping
// AudioContext.prototype.createGain before the app boots: the engine creates
// the master bus gain first, then the six stem gains in manifest order, so
// __e2eGainNodes[0] is the master and [1..6] are the stems. Polling their live
// gain.value proves applyStemGains really renders dbToLinear(trimDb + gainDb).

type StemState = {
  gainLinear: number;
  trimDb: number;
};

type PlaybackMetrics = {
  stems: Record<string, StemState>;
};

declare global {
  interface Window {
    __shizzlePlaybackHealth: { getMetrics(): PlaybackMetrics };
    __e2eGainNodes: GainNode[];
  }
}

const STEM_IDS = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'shizzle'] as const;
const TRACK_SLUG = 'e2e-trim';
/** Negative common trim, as the lossless intake computes it (D3: never a boost). */
const TRIM_DB = -12;
const dbToLinear = (db: number) => 10 ** (db / 20);
const TRIM_LINEAR = dbToLinear(TRIM_DB);
const within = (value: number, expected: number, tol = 0.01) => Math.abs(value - expected) < tol;

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

// 2 s of black 160x90 H.264, audio-less, faststart — small enough to inline so
// PlayerShell's bounded video staging succeeds without any network.
const VIDEO_MP4_BASE64 =
  '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PYAAAAAGQZpgL8HsAAAABkGagC/B7AAAAAZBmqAvwewAAAAGQZrAL8HsAAAABkGa4C/B7AAAAAZBmwAvwewAAAAGQZsgL8HsAAAABkGbQC/B7AAAAAZBm2AvwewAAAAGQZuAL8HsAAAABkGboC/B7AAAAAZBm8AvwewAAAAGQZvgL8HsAAAABkGaAC/B7AAAAAZBmiAvwewAAAAGQZpAL8HsAAAABkGaYC/B7AAAAAZBmoAvwewAAAAGQZqgL8HsAAAABkGawC/B7AAAAAZBmuAvwewAAAAGQZsAL8HsAAAABkGbIC/B7AAAAAZBm0AvwewAAAAGQZtgL8HsAAAABkGbgC/B7AAAAAZBm6AvwewAAAAGQZvAL8HsAAAABkGb4C/B7AAAAAZBmgAvwewAAAAGQZogL8HsAAAABkGaQC/B7AAAAAZBmmAvwewAAAAGQZqAL8HsAAAABkGaoC/B7AAAAAZBmsAvwewAAAAGQZrgL8HsAAAABkGbAC/B7AAAAAZBmyAvwewAAAAGQZtAL8HsAAAABkGbYC/B7AAAAAZBm4AvwewAAAAGQZugL8HsAAAABkGbwC/B7AAAAAZBm+AvwewAAAAGQZoAL8HsAAAABkGaIC/B7AAAAAZBmkAvwewAAAAGQZpgL8HsAAAABkGagC/B7AAAAAZBmqAvwewAAAAGQZrAL8HsAAAABkGa4C/B7AAAAAZBmwAvwewAAAAGQZsgL8HsAAAABkGbQCvB7AAAAAZBm2Anwew=';

async function metrics(page: Page): Promise<PlaybackMetrics> {
  return page.evaluate(() => window.__shizzlePlaybackHealth.getMetrics());
}

async function everyStem(page: Page, pick: (stem: StemState) => boolean): Promise<boolean> {
  const stems = Object.values((await metrics(page)).stems);
  return stems.length === 6 && stems.every(pick);
}

/** Live rendered gain.value of the six stem GainNodes, in manifest order
 *  (vocals first). Index 0 of __e2eGainNodes is the master bus, skipped. */
async function renderedStemGains(page: Page): Promise<number[]> {
  return page.evaluate(() => (window.__e2eGainNodes ?? []).slice(1, 7).map((g) => g.gain.value));
}

test('manifest trim survives store sync, fader moves, and Reset mixer', async ({ page }) => {
  test.setTimeout(120_000);

  const wav = wavBytes();
  await page.addInitScript(() => {
    localStorage.setItem('shizzle_token', 'e2e-token');
    // Record every GainNode the app creates so the spec can read the actual
    // rendered stem gains (see header comment for node ordering).
    const gains: GainNode[] = [];
    window.__e2eGainNodes = gains;
    const proto = window.AudioContext?.prototype;
    if (proto) {
      const original = proto.createGain;
      proto.createGain = function patchedCreateGain(this: AudioContext) {
        const node = original.call(this) as GainNode;
        gains.push(node);
        return node;
      };
    }
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
            title: 'Trim Probe',
            artist: 'E2E',
            slug: TRACK_SLUG,
            duration: 2,
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
        title: 'Trim Probe',
        artist: 'E2E',
        duration: 2,
        video: 'video.mp4',
        stems: STEM_IDS.map((id) => ({
          id,
          name: id,
          file: `stems/${id}.wav`,
          default_gain_db: TRIM_DB,
        })),
      },
    })
  );
  await page.route(`**/tracks/${TRACK_SLUG}/stems/*.wav`, (route) =>
    route.fulfill({ body: wav, contentType: 'audio/wav' })
  );
  await page.route(`**/tracks/${TRACK_SLUG}/video.mp4`, (route) =>
    route.fulfill({
      body: Buffer.from(VIDEO_MP4_BASE64, 'base64'),
      contentType: 'video/mp4',
    })
  );

  await page.goto('/');
  await page.getByRole('button', { name: 'Library' }).click();
  const heading = page.getByRole('heading', { name: 'Trim Probe', exact: true });
  await expect(heading).toBeAttached({ timeout: 10_000 });
  // Native click on the card (Radix drawer can clip cards out of viewport).
  await heading.evaluate((element) => {
    const card = element.closest<HTMLElement>('.cursor-pointer');
    if (!card) throw new Error('library card not found');
    card.click();
  });

  // Six stems loaded into the engine; the polls below hold across the
  // store-sync window (isReady flip + useAudioSync forwarding the 0 dB
  // faders). Before the fix this is exactly where the single gain slot was
  // stomped to unity.
  await page.waitForFunction(
    () => Object.keys(window.__shizzlePlaybackHealth.getMetrics().stems).length === 6,
    undefined,
    { timeout: 30_000 },
  );
  await expect
    .poll(async () =>
      everyStem(page, (stem) => stem.trimDb === TRIM_DB), // manifest trim kept
    )
    .toBe(true);
  await expect
    .poll(async () =>
      everyStem(page, (stem) => Math.abs(stem.gainLinear - 1) < 0.01), // fader at unity
    )
    .toBe(true);
  // The rendered GainNodes themselves must hold dbToLinear(TRIM_DB) after the
  // store sync — exactly the value the pre-fix code stomped to unity. The mute
  // check below also proves the isReady-guarded forwarding effect is live, so
  // the polls above really did observe the post-sync state.
  await expect
    .poll(async () => (await renderedStemGains(page)).every((v) => within(v, TRIM_LINEAR)))
    .toBe(true);

  await page.getByRole('button', { name: 'Mixer' }).click();

  // Silence still reports as zero through the fader-only metric.
  await page.getByRole('button', { name: 'vocals mute' }).click();
  await expect
    .poll(async () => (await metrics(page)).stems.vocals.gainLinear)
    .toBeLessThan(0.01);
  await page.getByRole('button', { name: 'vocals unmute' }).click();

  // A fader move must change the fader term without touching the trim.
  const vocalsFader = page.locator('[data-testid="stem-strip-vocals"] [role="slider"]');
  await vocalsFader.focus();
  await page.keyboard.press('End'); // +12 dB
  await expect
    .poll(async () => (await metrics(page)).stems.vocals.gainLinear)
    .toBeGreaterThan(dbToLinear(12) * 0.99);
  expect((await metrics(page)).stems.vocals.trimDb).toBe(TRIM_DB);
  // Rendered: trim -12 dB + fader +12 dB = dbToLinear(0) for vocals, while the
  // untouched stems keep rendering the trim alone.
  await expect
    .poll(async () => {
      const rendered = await renderedStemGains(page);
      return (
        rendered.length === 6 &&
        within(rendered[0], 1) &&
        rendered.slice(1).every((v) => within(v, TRIM_LINEAR))
      );
    })
    .toBe(true);

  // Reset mixer restores unity FADERS — the rendered GainNodes return to the
  // manifest trim (dbToLinear(TRIM_DB)), NOT to unity.
  await page.getByRole('button', { name: 'Reset mixer' }).click();
  await expect
    .poll(async () => everyStem(page, (stem) => Math.abs(stem.gainLinear - 1) < 0.01))
    .toBe(true);
  await expect
    .poll(async () => everyStem(page, (stem) => stem.trimDb === TRIM_DB))
    .toBe(true);
  await expect
    .poll(async () => (await renderedStemGains(page)).every((v) => within(v, TRIM_LINEAR)))
    .toBe(true);
});
