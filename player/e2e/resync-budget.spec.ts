import { expect, test, type Page } from '@playwright/test';

// Regression tests for the WebKit hard-seek loop found in production playback
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
// The engine now does not judge sync while a stem seek is landing or before
// its own correction has settled, backs off while corrections do not hold,
// and aims the next one ahead of the master by the lag the previous one left
// behind.
//
// Chromium lands buffered seeks in a few milliseconds, so these specs give
// the stem elements late-landing seeks by wrapping HTMLMediaElement's
// currentTime and seeking accessors before the app boots: a seek on a playing
// audio element is applied `latencyMs` late, and until then the element
// reports the target and `seeking === true`, as the media element spec
// requires. This emulates the timing only; `seeking`/`seeked` events and
// readyState changes still come from the delayed native seek.
//
// Runs fully offline against the local dev server (same harness style as
// manifest-trim.spec.ts and space-shortcut.spec.ts).

type StemState = { skewMs: number | null; hardSeeks: number };
type PlaybackMetrics = {
  stems: Record<string, StemState>;
  health: { status: string; recoveryAttempts: number };
};

type SeekProbeOptions = {
  latencyMs: number;
  /** When set, every late seek lands this far behind the video wherever it
   *  was aimed, so no lead can make a correction hold. */
  landBehindVideoSec: number | null;
};

declare global {
  interface Window {
    __shizzlePlaybackHealth: { getMetrics(): PlaybackMetrics };
    __e2eSeekProbe: {
      /** Seeks issued on a stem whose previous seek had not landed yet. */
      stackedSeeks: number;
      /** performance.now() of each late seek issued on the first stem. */
      correctionTimes: number[];
      /** Moves every stem behind the master without going through the engine. */
      knockStemsBehind(seconds: number): number;
    };
  }
}

const STEM_IDS = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'shizzle'] as const;
const TRACK_SLUG = 'e2e-resync-budget';
const TRACK_DURATION_SECONDS = 30;
/** The production playback contract: settled within 50 ms inside 3 s. */
const SETTLED_OFFSET_MS = 50;
const SETTLE_DEADLINE_MS = 3000;

/** Minimal valid PCM16 mono 8 kHz WAV; the engine only needs it to play. */
function wavBytes(seconds: number): Buffer {
  const sampleRate = 8000;
  const channels = 1;
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

// 30 s of black 160x90 H.264 (baseline profile), audio-less, faststart,
// generated with:
//   ffmpeg -f lavfi -i color=c=black:s=160x90:r=10:d=30 -pix_fmt yuv420p \
//     -c:v libx264 -profile:v baseline -level 3.0 -movflags +faststart -an out.mp4
const VIDEO_MP4_BASE64 =
  'AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAfZbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAdTAAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAABwN0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAdTAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAKAAAABaAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAHUwAAAAAAABAAAAAAZ7bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAEsABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAAGJm1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAABeZzdGJsAAAAunN0c2QAAAAAAAAAAQAAAKphdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAKAAWgBIAAAASAAAAAAAAAABFUxhdmM2Mi4yOC4xMDEgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAAMGF2Y0MBQsAe/+EAGGdCwB7ZAo35MBEAAAMAAQAAAwAUDxYuSAEABWjLg8sgAAAAEHBhc3AAAAABAAAAAQAAABRidHJ0AAAAAAAAA+MAAAAAAAAAGHN0dHMAAAAAAAAAAQAAASwAAAQAAAAAGHN0c3MAAAAAAAAAAgAAAAEAAAD7AAAAHHN0c2MAAAAAAAAAAQAAAAEAAAEsAAAAAQAABMRzdHN6AAAAAAAAAAAAAAEsAAACsgAAAAoAAAALAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAD0AAAAKAAAACwAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAAFHN0Y28AAAAAAAAAAQAACAkAAABidWR0YQAAAFptZXRhAAAAAAAAACFoZGxyAAAAAAAAAABtZGlyYXBwbAAAAAAAAAAAAAAAAC1pbHN0AAAAJal0b28AAAAdZGF0YQAAAAEAAAAATGF2ZjYyLjEyLjEwMQAAAAhmcmVlAAAOnW1kYXQAAAJxBgX//23cRem95tlIt5Ys2CDZI+7veDI2NCAtIGNvcmUgMTY1IHIzMjIzIDA0ODBjYjAgLSBILjI2NC9NUEVHLTQgQVZDIGNvZGVjIC0gQ29weWxlZnQgMjAwMy0yMDI1IC0gaHR0cDovL3d3dy52aWRlb2xhbi5vcmcveDI2NC5odG1sIC0gb3B0aW9uczogY2FiYWM9MCByZWY9MyBkZWJsb2NrPTE6MDowIGFuYWx5c2U9MHgxOjB4MTExIG1lPWhleCBzdWJtZT03IHBzeT0xIHBzeV9yZD0xLjAwOjAuMDAgbWl4ZWRfcmVmPTEgbWVfcmFuZ2U9MTYgY2hyb21hX21lPTEgdHJlbGxpcz0xIDh4OGRjdD0wIGNxbT0wIGRlYWR6b25lPTIxLDExIGZhc3RfcHNraXA9MSBjaHJvbWFfcXBfb2Zmc2V0PS0yIHRocmVhZHM9MyBsb29rYWhlYWRfdGhyZWFkcz0xIHNsaWNlZF90aHJlYWRzPTAgbnI9MCBkZWNpbWF0ZT0xIGludGVybGFjZWQ9MCBibHVyYXlfY29tcGF0PTAgY29uc3RyYWluZWRfaW50cmE9MCBiZnJhbWVzPTAgd2VpZ2h0cD0wIGtleWludD0yNTAga2V5aW50X21pbj0xMCBzY2VuZWN1dD00MCBpbnRyYV9yZWZyZXNoPTAgcmNfbG9va2FoZWFkPTQwIHJjPWNyZiBtYnRyZWU9MSBjcmY9MjMuMCBxY29tcD0wLjYwIHFwbWluPTAgcXBtYXg9NjkgcXBzdGVwPTQgaXBfcmF0aW89MS40MCBhcT0xOjEuMDAAgAAAADlliIQP8mKAAMPsnJycnJycnJyddddddddddddddddddddddddddddddddddddddddddddddddddeAAAAAGQZo4H+D2AAAAB0GaVAf4PYAAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAAOWWIggEvJigADijJycnJycnJycnXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXgAAAAZBmjgf4PYAAAAHQZpUB/g9gAAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA7wewAAAAGQZogN8Hs';

async function metrics(page: Page): Promise<PlaybackMetrics> {
  return page.evaluate(() => window.__shizzlePlaybackHealth.getMetrics());
}

async function videoState(page: Page): Promise<{ time: number; ended: boolean }> {
  return page
    .locator('video')
    .evaluate((video: HTMLVideoElement) => ({ time: video.currentTime, ended: video.ended }));
}

function maxHardSeeks(m: PlaybackMetrics): number {
  return Math.max(...Object.values(m.stems).map((stem) => stem.hardSeeks));
}

function maxOffsetMs(m: PlaybackMetrics): number {
  return Math.max(...Object.values(m.stems).map((stem) => Math.abs(stem.skewMs ?? Infinity)));
}

/** Boots the app with late-landing stem seeks and starts the probe track. */
async function playWithLateSeeks(page: Page, options: SeekProbeOptions): Promise<void> {
  const wav = wavBytes(TRACK_DURATION_SECONDS);
  await page.addInitScript(({ latencyMs, landBehindVideoSec }) => {
    localStorage.setItem('shizzle_token', 'e2e-token');

    const proto = HTMLMediaElement.prototype;
    const currentTime = Object.getOwnPropertyDescriptor(proto, 'currentTime')!;
    const seeking = Object.getOwnPropertyDescriptor(proto, 'seeking')!;
    const nativePlay = proto.play;
    const pending = new Map<HTMLMediaElement, { target: number; timer: number }>();
    const stems: HTMLAudioElement[] = [];
    const probe = {
      stackedSeeks: 0,
      correctionTimes: [] as number[],
      knockStemsBehind(seconds: number): number {
        for (const el of stems) {
          currentTime.set!.call(el, Math.max(0, (currentTime.get!.call(el) as number) - seconds));
        }
        return stems.length;
      },
    };
    window.__e2eSeekProbe = probe;

    proto.play = function patchedPlay(this: HTMLMediaElement) {
      if (this instanceof HTMLAudioElement && !stems.includes(this)) stems.push(this);
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
        if (prior || (seeking.get!.call(this) as boolean)) probe.stackedSeeks += 1;
        if (prior) window.clearTimeout(prior.timer);
        if (this === stems[0]) probe.correctionTimes.push(performance.now());
        const timer = window.setTimeout(() => {
          pending.delete(this);
          const video = document.querySelector('video');
          const landAt =
            landBehindVideoSec !== null && video
              ? Math.max(0, video.currentTime - landBehindVideoSec)
              : value;
          currentTime.set!.call(this, landAt);
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
  }, options);

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
  await expect
    .poll(async () => (await videoState(page)).time, { timeout: 10_000, intervals: [100] })
    .toBeGreaterThan(0.1);
}

test.describe('while-playing resync budget (WebKit hard-seek loop)', () => {
  test('stems knocked behind the master are back in sync within the contract when seeks land late', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    /** Longer than one 100 ms watchdog tick and than the 40 ms hard threshold. */
    await playWithLateSeeks(page, { latencyMs: 150, landBehindVideoSec: null });

    // Settled, healthy playback first, so everything after the knock is the
    // engine's response to it and nothing else. Startup itself may take a few
    // corrections here: the first seek refetches through the route handler
    // and lands much later than the ones after it.
    await expect
      .poll(async () => (await metrics(page)).health.status, { timeout: 10_000, intervals: [100] })
      .toBe('healthy');
    // Then let sync hold (RESYNC_HOLD_MS is 2 s), so the knock is a new event
    // and not a startup correction that failed to hold.
    await page.waitForTimeout(2500);
    const before = await metrics(page);
    expect(before.health.status).toBe('healthy');
    expect(Object.keys(before.stems)).toHaveLength(6);
    const stackedBefore = await page.evaluate(() => window.__e2eSeekProbe.stackedSeeks);

    // Put the ensemble where a late-landing WebKit start leaves it: together,
    // and well behind the master.
    expect(await page.evaluate(() => window.__e2eSeekProbe.knockStemsBehind(0.3))).toBe(6);

    // The production contract: back within 50 ms of the master inside 3 s.
    await expect
      .poll(
        async () => {
          const m = await metrics(page);
          return maxHardSeeks(m) > maxHardSeeks(before) && maxOffsetMs(m) <= SETTLED_OFFSET_MS && m.health.status === 'healthy';
        },
        { timeout: SETTLE_DEADLINE_MS, intervals: [100] },
      )
      .toBe(true);

    // And it stays there without further seeking. The pre-fix engine issued
    // about fifty hard seeks per stem in a window this long and never settled.
    await page.waitForTimeout(2000);
    const after = await metrics(page);
    expect((await videoState(page)).ended).toBe(false);
    expect(maxHardSeeks(after) - maxHardSeeks(before)).toBeLessThanOrEqual(3);
    expect(await page.evaluate(() => window.__e2eSeekProbe.stackedSeeks)).toBe(stackedBefore);
    expect(stackedBefore).toBe(0);
    expect(maxOffsetMs(after)).toBeLessThanOrEqual(SETTLED_OFFSET_MS);
    expect(after.health.status).toBe('healthy');
  });

  test('a seek that takes longer than the stall threshold to land is not treated as a stalled stem', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    // Longer than the 1000 ms stem-stall threshold, shorter than the 1500 ms
    // landing timeout: while it lands, the stem's clock sits at the target.
    await playWithLateSeeks(page, { latencyMs: 1200, landBehindVideoSec: null });
    expect(await page.evaluate(() => window.__e2eSeekProbe.knockStemsBehind(0.3))).toBe(6);

    const observeMs = 6000;
    const start = await videoState(page);
    await page.waitForTimeout(observeMs);
    const after = await metrics(page);
    const end = await videoState(page);

    expect(end.ended).toBe(false);
    expect(end.time - start.time).toBeGreaterThan((observeMs / 1000) * 0.8);
    // At least one correction was issued and had to land, so the stall
    // detector was exercised.
    expect(maxHardSeeks(after)).toBeGreaterThanOrEqual(1);
    // Recovering from a landing seek would hard-seek on top of it and reset
    // the budget; before this was exempted the engine recovered about once a
    // second here.
    expect(after.health.recoveryAttempts).toBe(0);
    expect(await page.evaluate(() => window.__e2eSeekProbe.stackedSeeks)).toBe(0);
    // Landing (1.2 s) plus settle (0.4 s) allows at most four in the window.
    expect(maxHardSeeks(after)).toBeLessThanOrEqual(4);
  });

  test('corrections that never hold are spaced out by a doubling backoff', async ({ page }) => {
    test.setTimeout(120_000);
    // Every late seek lands 200 ms behind the video wherever it was aimed, so
    // the learned lead cannot make a correction hold.
    await playWithLateSeeks(page, { latencyMs: 150, landBehindVideoSec: 0.2 });
    expect(await page.evaluate(() => window.__e2eSeekProbe.knockStemsBehind(0.3))).toBe(6);

    await expect
      .poll(async () => page.evaluate(() => window.__e2eSeekProbe.correctionTimes.length), {
        timeout: 15_000,
        intervals: [200],
      })
      .toBeGreaterThanOrEqual(5);
    const times = await page.evaluate(() => window.__e2eSeekProbe.correctionTimes.slice(0, 5));
    const intervals = times.slice(1).map((time, index) => time - times[index]);
    const after = await metrics(page);

    // A recovery would have restarted the budget and invalidated the spacing.
    expect(after.health.recoveryAttempts).toBe(0);
    expect((await videoState(page)).ended).toBe(false);
    expect(await page.evaluate(() => window.__e2eSeekProbe.stackedSeeks)).toBe(0);
    // Spacing after the nth correction is at least 400 ms * 2^(n-1), and a
    // correction is issued on the first watchdog tick that allows it. The
    // first interval is bounded below by landing plus settle (550 ms).
    const minimumMs = [400, 800, 1600, 3200];
    const slackMs = 700;
    intervals.forEach((interval, index) => {
      expect(interval, `interval ${index + 1}`).toBeGreaterThanOrEqual(minimumMs[index] - 20);
      expect(interval, `interval ${index + 1}`).toBeLessThanOrEqual(Math.max(minimumMs[index], 550) + slackMs);
    });
  });
});
