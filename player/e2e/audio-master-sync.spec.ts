import { expect, test, type Page } from '@playwright/test';

// Audio-master synchronization specs.
//
// The engine never seeks or rate-changes a playing stem. On WebKit — every
// iPad browser — each such operation freezes the stem's clock for about a
// third of a second while a stem left alone holds a constant offset to the
// millisecond (iPad probe, 2026-10-02). Correction is therefore:
//   1. each stem's Web Audio DelayNode holds it back to the latest-running
//      stem, so all stems sound together without touching their elements;
//   2. the silent video is seeked to the stems' audible position
//      (element clock minus delay), aimed ahead by what its previous seek
//      was measured to lose, inside the budget/backoff rules;
//   3. after a start, user seek or recovery, stem offsets are not trusted
//      until every stem clock has run at real-time speed for a window.
//
// These specs emulate the measured WebKit behaviour in Chromium by wrapping
// HTMLMediaElement accessors before the app boots:
//   - each stem's clock is virtual: on play() it freezes for startFreezeMs
//     plus its index * startStaggerMs (the iPad start freeze and 16 ms
//     per-stem stagger), then runs at exactly 1x;
//   - a seek on a playing audio element is applied latencyMs late and until
//     then reports its target with `seeking === true`, as the media element
//     spec requires — so it lands behind wherever it was aimed;
//   - a video seek may land late and/or a fixed distance behind its aim (or
//     behind the stems), to exercise the learned lead and the backoff;
//   - playbackRate changes on audio elements are counted (they must never
//     happen).
//
// Runs fully offline against the local dev server (same harness style as
// manifest-trim.spec.ts and space-shortcut.spec.ts), with Range-supporting
// synthetic WAV stems and the inline 30 s H.264 video.

type StemState = {
  skewMs: number | null;
  hardSeeks: number;
  paused: boolean;
  delayMs: number;
};
type PlaybackMetrics = {
  stems: Record<string, StemState>;
  videoSeeks: number;
  health: { status: string; recoveryAttempts: number };
  incidents: Array<{ code: string }>;
};

type EmulationOptions = {
  /** How long each stem's clock stays frozen after play() (WebKit start freeze). */
  startFreezeMs: number;
  /** Additional per-stem start delay: stem i starts i * this much later. */
  startStaggerMs: number;
  /** How late a seek on a playing audio element lands. */
  latencyMs: number;
  /** How late a video seek lands (0 keeps the browser's native behavior). */
  videoLatencyMs: number;
  /** When set, every late video seek lands this far behind where it was aimed. */
  videoLandBehindAimSec: number | null;
  /** When set, every late video seek lands this far behind the stems'
   *  audible position regardless of where it was aimed, so no lead can make
   *  a correction hold. */
  videoLandBehindStemsSec: number | null;
  /** Seeks on a paused stem land late too (default: only on a playing one). */
  latePausedSeeks?: boolean;
};

declare global {
  interface Window {
    __shizzlePlaybackHealth: { getMetrics(): PlaybackMetrics };
    __e2eAudioMasterProbe: {
      latencyMs: number;
      /** Actual playbackRate changes applied to audio elements. */
      rateChanges: number;
      /** currentTime assignments to audio elements (deferred or immediate). */
      stemSeeks: number;
      /** performance.now() of each video currentTime assignment. */
      videoSeekTimes: number[];
      /** Stem ids in engine play order. */
      stemOrder(): string[];
    };
  }
}

const STEM_IDS = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'shizzle'] as const;
const TRACK_SLUG = 'e2e-audio-master';
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
  'AAAAIGZ0eXBpc29tAAACAGlzb21pc28yYXZjMW1wNDEAAAfZbW9vdgAAAGxtdmhkAAAAAAAAAAAAAAAAAAAD6AAAdTAAAQAAAQAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgAABwN0cmFrAAAAXHRraGQAAAADAAAAAAAAAAAAAAABAAAAAAAAdTAAAAAAAAAAAAAAAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAABAAAAAAAAAAAAAAAAAABAAAAAAKAAAABaAAAAAAAkZWR0cwAAABxlbHN0AAAAAAAAAAEAAHUwAAAAAAABAAAAAAZ7bWRpYQAAACBtZGhkAAAAAAAAAAAAAAAAAAAoAAAEsABVxAAAAAAALWhkbHIAAAAAAAAAAHZpZGUAAAAAAAAAAAAAAABWaWRlb0hhbmRsZXIAAAAGJm1pbmYAAAAUdm1oZAAAAAEAAAAAAAAAAAAAACRkaW5mAAAAHGRyZWYAAAAAAAAAAQAAAAx1cmwgAAAAAQAABeZzdGJsAAAAunN0c2QAAAAAAAAAAQAAAKphdmMxAAAAAAAAAAEAAAAAAAAAAAAAAAAAAAAAAKAAWgBIAAAASAAAAAAAAAABFUxhdmM2Mi4yOC4xMDEgbGlieDI2NAAAAAAAAAAAAAAAGP//AAAAMGF2Y0MBQsAe/+EAGGdCwB7ZAo35MBEAAAMAAQAAAwAUDxYuSAEABWjLg8sgAAAAEHBhc3AAAAABAAAAAQAAABRidHJ0AAAAAAAAA+MAAAAAAAAAGHN0dHMAAAAAAAAAAQAAASwAAAQAAAAAGHN0c3MAAAAAAAAAAgAAAAEAAAD7AAAAHHN0c2MAAAAAAAAAAQAAAAEAAAEsAAAAAQAABMRzdHN6AAAAAAAAAAAAAAEsAAACsgAAAAoAAAALAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAD0AAAAKAAAACwAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoAAAAKAAAACgAAAAoA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AAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA/wewAAAAGQZogP8HsAAAABkGaQD/B7AAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAAOWWIggEvJigADijJycnJycnJycnXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXgAAAAZBmjgf4PYAAAAHQZpUB/g9gAAAAAZBmmA/wewAAAAGQZqAP8HsAAAABkGaoD/B7AAAAAZBmsA/wewAAAAGQZrgP8HsAAAABkGbAD/B7AAAAAZBmyA/wewAAAAGQZtAP8HsAAAABkGbYD/B7AAAAAZBm4A/wewAAAAGQZugP8HsAAAABkGbwD/B7AAAAAZBm+A/wewAAAAGQZoAP8HsAAAABkGaID/B7AAAAAZBmkA/wewAAAAGQZpgP8HsAAAABkGagD/B7AAAAAZBmqA/wewAAAAGQZrAP8HsAAAABkGa4D/B7AAAAAZBmwA/wewAAAAGQZsgP8HsAAAABkGbQD/B7AAAAAZBm2A/wewAAAAGQZuAP8HsAAAABkGboD/B7AAAAAZBm8A/wewAAAAGQZvgP8HsAAAABkGaAD/B7AAAAAZBmiA/wewAAAAGQZpAP8HsAAAABkGaYD/B7AAAAAZBmoA/wewAAAAGQZqgP8HsAAAABkGawD/B7AAAAAZBmuA/wewAAAAGQZsAP8HsAAAABkGbID/B7AAAAAZBm0A/wewAAAAGQZtgP8HsAAAABkGbgD/B7AAAAAZBm6A/wewAAAAGQZvAP8HsAAAABkGb4D/B7AAAAAZBmgA7wewAAAAGQZogN8Hs';

async function metrics(page: Page): Promise<PlaybackMetrics> {
  return page.evaluate(() => window.__shizzlePlaybackHealth.getMetrics());
}

async function probeCount(page: Page, key: 'rateChanges' | 'stemSeeks'): Promise<number> {
  return page.evaluate((k) => window.__e2eAudioMasterProbe[k], key);
}

async function videoSeekTimes(page: Page): Promise<number[]> {
  return page.evaluate(() => window.__e2eAudioMasterProbe.videoSeekTimes);
}

async function stemOrder(page: Page): Promise<string[]> {
  return page.evaluate(() => window.__e2eAudioMasterProbe.stemOrder());
}

async function videoState(page: Page): Promise<{ time: number; ended: boolean }> {
  return page
    .locator('video')
    .evaluate((video: HTMLVideoElement) => ({ time: video.currentTime, ended: video.ended }));
}

function stemList(m: PlaybackMetrics): StemState[] {
  return STEM_IDS.map((id) => m.stems[id]);
}

/** Largest absolute audible stem-to-video offset, in ms. */
function maxOffsetMs(m: PlaybackMetrics): number {
  return Math.max(...stemList(m).map((stem) => Math.abs(stem.skewMs ?? Infinity)));
}

/** Largest audible stem-to-stem separation, in ms. */
function spreadMs(m: PlaybackMetrics): number {
  const offsets = stemList(m).map((stem) => stem.skewMs ?? 0);
  return Math.max(...offsets) - Math.min(...offsets);
}

function maxHardSeeks(m: PlaybackMetrics): number {
  return Math.max(...stemList(m).map((stem) => stem.hardSeeks));
}

function settled(m: PlaybackMetrics): boolean {
  return maxOffsetMs(m) <= SETTLED_OFFSET_MS && m.health.status === 'healthy';
}

/** Boots the app with the WebKit emulation and starts the probe track.
 *  Returns performance.now() (page clock) at the Play click. */
async function playWithEmulation(page: Page, options: EmulationOptions): Promise<number> {
  const wav = wavBytes(TRACK_DURATION_SECONDS);
  await page.addInitScript((opts) => {
    localStorage.setItem('shizzle_token', 'e2e-token');

    const proto = HTMLMediaElement.prototype;
    const currentTime = Object.getOwnPropertyDescriptor(proto, 'currentTime')!;
    const seeking = Object.getOwnPropertyDescriptor(proto, 'seeking')!;
    const rate = Object.getOwnPropertyDescriptor(proto, 'playbackRate')!;
    const nativePlay = proto.play;
    const nativePause = proto.pause;

    // Virtual stem clocks: pos (seconds) plus elapsed real time since startAt,
    // frozen before startAt. A pending seek reports its target, as the media
    // element spec requires while `seeking` is true.
    const stems: Array<{ el: HTMLAudioElement; name: string; pos: number; startAt: number | null }> = [];
    const pending = new Map<HTMLMediaElement, { target: number; timer: number }>();
    const probe = {
      latencyMs: opts.latencyMs,
      rateChanges: 0,
      stemSeeks: 0,
      videoSeekTimes: [] as number[],
      stemOrder(): string[] {
        return stems.map((s) => s.name);
      },
    };
    window.__e2eAudioMasterProbe = probe;

    function stemOf(el: HTMLMediaElement) {
      return stems.find((s) => s.el === el) ?? null;
    }
    function stemVirtualNow(s: { el: HTMLAudioElement; pos: number; startAt: number | null }): number {
      if (pending.has(s.el)) return pending.get(s.el)!.target;
      if (s.el.paused || s.startAt === null) return s.pos;
      const now = performance.now();
      return now < s.startAt ? s.pos : s.pos + (now - s.startAt) / 1000;
    }
    /** Lowest raw stem clock: the position the engine aligns the video to. */
    function latestStemTime(): number {
      return Math.min(...stems.map((s) => stemVirtualNow(s)));
    }

    proto.play = function patchedPlay(this: HTMLMediaElement) {
      let s = stemOf(this);
      if (this instanceof HTMLAudioElement && !s) {
        const file = decodeURIComponent(
          new URL(this.currentSrc || this.src || 'about:blank').pathname.split('/').pop() ?? '',
        );
        s = { el: this as HTMLAudioElement, name: file.replace(/\.wav$/, ''), pos: 0, startAt: null };
        s.pos = currentTime.get!.call(this) as number;
        stems.push(s);
      }
      if (s && this.paused) {
        // Rebase the virtual clock, then freeze it for the WebKit start
        // stagger: each stem's clock begins running later than the previous.
        s.pos = stemVirtualNow(s);
        s.startAt =
          performance.now() + opts.startFreezeMs + stems.indexOf(s) * opts.startStaggerMs;
      }
      return nativePlay.call(this);
    };

    proto.pause = function patchedPause(this: HTMLMediaElement) {
      const s = stemOf(this);
      // Freeze the virtual clock at its current value, not at the base it
      // last rebased from: a paused element keeps reporting the time it had
      // when it stopped.
      if (s && !this.paused) {
        s.pos = stemVirtualNow(s);
        s.startAt = null;
      }
      return nativePause.call(this);
    };

    Object.defineProperty(proto, 'currentTime', {
      configurable: true,
      enumerable: currentTime.enumerable,
      get(this: HTMLMediaElement) {
        const held = pending.get(this);
        if (held) return held.target;
        const s = stemOf(this);
        if (s) return stemVirtualNow(s);
        return currentTime.get!.call(this) as number;
      },
      set(this: HTMLMediaElement, value: number) {
        const prior = pending.get(this);
        if (prior) {
          window.clearTimeout(prior.timer);
          pending.delete(this);
        }
        const s = stemOf(this);
        if (!s) {
          // The video master: optionally land its seeks late and behind.
          probe.videoSeekTimes.push(performance.now());
          const emulate = opts.videoLatencyMs > 0 || opts.videoLandBehindAimSec !== null || opts.videoLandBehindStemsSec !== null;
          if (!emulate) {
            currentTime.set!.call(this, value);
            return;
          }
          const timer = window.setTimeout(() => {
            pending.delete(this);
            let landAt = value;
            if (opts.videoLandBehindStemsSec !== null) {
              landAt = Math.max(0, latestStemTime() - opts.videoLandBehindStemsSec);
            } else if (opts.videoLandBehindAimSec !== null) {
              landAt = Math.max(0, value - opts.videoLandBehindAimSec);
            }
            currentTime.set!.call(this, landAt);
          }, opts.videoLatencyMs);
          pending.set(this, { target: value, timer });
          return;
        }
        probe.stemSeeks += 1;
        if (this.paused && !opts.latePausedSeeks) {
          // A seek on a paused stem lands at once.
          s.pos = value;
          currentTime.set!.call(this, value);
          return;
        }
        const timer = window.setTimeout(() => {
          pending.delete(this);
          s.pos = value;
          s.startAt = performance.now();
          currentTime.set!.call(this, value);
        }, probe.latencyMs);
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

    Object.defineProperty(proto, 'playbackRate', {
      configurable: true,
      enumerable: rate.enumerable,
      get(this: HTMLMediaElement) {
        return rate.get!.call(this) as number;
      },
      set(this: HTMLMediaElement, value: number) {
        if (this instanceof HTMLAudioElement && value !== (rate.get!.call(this) as number)) {
          probe.rateChanges += 1;
        }
        rate.set!.call(this, value);
      },
    });
  }, options as unknown as Record<string, unknown>);

  await page.route('**/api/media/session', (route) =>
    route.fulfill({ json: { cloudfront: false } })
  );
  await page.route('**/api/library', (route) =>
    route.fulfill({
      json: {
        tracks: [
          {
            id: TRACK_SLUG,
            title: 'Audio Master Probe',
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
        title: 'Audio Master Probe',
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
  const heading = page.getByRole('heading', { name: 'Audio Master Probe', exact: true });
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
  const playedAt = await page.evaluate(() => performance.now());
  await expect
    .poll(async () => (await videoState(page)).time, { timeout: 10_000, intervals: [100] })
    .toBeGreaterThan(0.1);
  return playedAt;
}

/** Polls a boolean condition with a deadline relative to a page-clock
 *  timestamp (e.g. the Play click), so "within 3 s of Play" means that. */
async function pollSettledRelativeTo(
  page: Page,
  sincePageMs: number,
  deadlineMs: number,
): Promise<void> {
  const startedAt = await page.evaluate(() => performance.now());
  const remainingMs = deadlineMs - Math.max(0, startedAt - sincePageMs);
  await expect
    .poll(async () => settled(await metrics(page)), {
      timeout: Math.max(500, remainingMs),
      intervals: [100],
    })
    .toBe(true);
}

test.describe('audio-master alignment (WebKit timing emulation)', () => {
  test('staggered start: delays line the stems up and nothing is seeked or rate-changed', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    // Defaults emulate the iPad measurements: ~300 ms start freeze, 16 ms
    // per-stem stagger (80 ms spread), late-landing seeks on playing stems.
    const playedAt = await playWithEmulation(page, {
      startFreezeMs: 300,
      startStaggerMs: 16,
      latencyMs: 150,
      videoLatencyMs: 0,
      videoLandBehindAimSec: null,
      videoLandBehindStemsSec: null,
    });

    await pollSettledRelativeTo(page, playedAt, SETTLE_DEADLINE_MS);
    const m = await metrics(page);

    // The delays are the alignment: stem i started i * 16 ms after stem 0's
    // chain position, so the first-started stem is held back by 5 * 16 ms
    // and the last-started by 0.
    expect(await stemOrder(page)).toEqual([...STEM_IDS]);
    STEM_IDS.forEach((id, i) => {
      expect(m.stems[id].delayMs, `${id} delayMs`).toBeCloseTo((STEM_IDS.length - 1 - i) * 16, -1);
    });
    expect(spreadMs(m)).toBeLessThanOrEqual(10);
    // Nothing was seeked or rate-changed after playback started: the whole
    // correction was delays plus one move of the silent video.
    expect(maxHardSeeks(m)).toBe(0);
    expect(await probeCount(page, 'stemSeeks')).toBe(0);
    expect(await probeCount(page, 'rateChanges')).toBe(0);
    expect(m.health.recoveryAttempts).toBe(0);
    expect((await videoState(page)).ended).toBe(false);
  });

  test('stems left behind by the start are corrected by seeking the video, not the stems', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    const playedAt = await playWithEmulation(page, {
      startFreezeMs: 300,
      startStaggerMs: 16,
      latencyMs: 150,
      videoLatencyMs: 0,
      videoLandBehindAimSec: null,
      videoLandBehindStemsSec: null,
    });

    // The start freeze leaves the ensemble 300+ ms behind the video: far
    // beyond the 40 ms threshold. The engine must move the video back to
    // the stems' audible position and leave every stem element alone.
    await expect
      .poll(
        async () => {
          const m = await metrics(page);
          return m.videoSeeks >= 1 && settled(m);
        },
        { timeout: SETTLE_DEADLINE_MS + 800, intervals: [100] },
      )
      .toBe(true);
    await pollSettledRelativeTo(page, playedAt, SETTLE_DEADLINE_MS);

    const m = await metrics(page);
    expect(m.videoSeeks).toBeGreaterThanOrEqual(1);
    expect(maxHardSeeks(m)).toBe(0);
    expect(await probeCount(page, 'stemSeeks')).toBe(0);
    expect(await probeCount(page, 'rateChanges')).toBe(0);
    expect(m.health.recoveryAttempts).toBe(0);
    expect((await videoState(page)).ended).toBe(false);
  });

  test('a video whose seeks land behind the aim converges by learning the lead', async ({ page }) => {
    test.setTimeout(120_000);
    await playWithEmulation(page, {
      startFreezeMs: 300,
      startStaggerMs: 16,
      latencyMs: 150,
      // Every video seek lands 150 ms late and 250 ms behind where it was
      // aimed: without a learned lead each correction leaves the video
      // behind the stems again.
      videoLatencyMs: 150,
      videoLandBehindAimSec: 0.25,
      videoLandBehindStemsSec: null,
    });

    await expect
      .poll(
        async () => {
          const m = await metrics(page);
          return m.videoSeeks >= 2 && settled(m);
        },
        { timeout: 8000, intervals: [200] },
      )
      .toBe(true);

    // And it stays converged: the learned lead absorbs the landing loss, so
    // no further correction is needed.
    await page.waitForTimeout(2000);
    const m = await metrics(page);
    expect(settled(m)).toBe(true);
    expect(maxOffsetMs(m)).toBeLessThanOrEqual(SETTLED_OFFSET_MS);
    expect(maxHardSeeks(m)).toBe(0);
    expect(await probeCount(page, 'stemSeeks')).toBe(0);
    expect(await probeCount(page, 'rateChanges')).toBe(0);
    expect((await videoState(page)).ended).toBe(false);
  });

  test('a video that always lands behind the stems is corrected on a doubling backoff', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    await playWithEmulation(page, {
      startFreezeMs: 300,
      startStaggerMs: 16,
      latencyMs: 150,
      // Every video seek lands behind the stems' audible position wherever
      // it was aimed, so no lead can make a correction hold.
      videoLatencyMs: 50,
      videoLandBehindAimSec: null,
      videoLandBehindStemsSec: 0.25,
    });

    await expect
      .poll(async () => (await videoSeekTimes(page)).length, {
        timeout: 15_000,
        intervals: [200],
      })
      .toBeGreaterThanOrEqual(5);
    const times = (await videoSeekTimes(page)).slice(0, 5);
    const intervals = times.slice(1).map((time, index) => time - times[index]);
    const m = await metrics(page);

    // Spacing after the nth correction is at least 400 ms * 2^(n-1); a
    // correction is issued soon after the budget allows it (the upper bound
    // only shows the engine does not stop correcting).
    const minimumMs = [400, 800, 1600, 3200];
    const slackMs = 1500;
    intervals.forEach((interval, index) => {
      expect(interval, `interval ${index + 1}`).toBeGreaterThanOrEqual(minimumMs[index] - 25);
      expect(interval, `interval ${index + 1}`).toBeLessThanOrEqual(minimumMs[index] + slackMs);
    });
    // While the video cannot converge, the stems are still never touched.
    expect(maxHardSeeks(m)).toBe(0);
    expect(await probeCount(page, 'stemSeeks')).toBe(0);
    expect(await probeCount(page, 'rateChanges')).toBe(0);
    expect(m.health.recoveryAttempts).toBe(0);
    expect((await videoState(page)).ended).toBe(false);
  });

  test('steady playback after alignment stays quiet for 10 s', async ({ page }) => {
    test.setTimeout(120_000);
    await playWithEmulation(page, {
      startFreezeMs: 300,
      startStaggerMs: 16,
      latencyMs: 150,
      videoLatencyMs: 0,
      videoLandBehindAimSec: null,
      videoLandBehindStemsSec: null,
    });

    await expect
      .poll(async () => settled(await metrics(page)), { timeout: 5000, intervals: [100] })
      .toBe(true);
    const before = await metrics(page);
    const seeksBefore = await probeCount(page, 'stemSeeks');

    await page.waitForTimeout(10_000);

    const after = await metrics(page);
    expect(after.health.status).toBe('healthy');
    expect(after.videoSeeks).toBe(before.videoSeeks);
    STEM_IDS.forEach((id) => {
      expect(after.stems[id].hardSeeks, `${id} hardSeeks`).toBe(before.stems[id].hardSeeks);
      expect(after.stems[id].delayMs, `${id} delayMs`).toBe(before.stems[id].delayMs);
    });
    expect(await probeCount(page, 'stemSeeks')).toBe(seeksBefore);
    expect(await probeCount(page, 'rateChanges')).toBe(0);
    expect(maxOffsetMs(after)).toBeLessThanOrEqual(SETTLED_OFFSET_MS);
    expect((await videoState(page)).ended).toBe(false);
  });

  test('a user seek while playing and a pause/resume resettle; the pause/resume seeks no stem', async ({
    page,
  }) => {
    test.setTimeout(120_000);
    await playWithEmulation(page, {
      startFreezeMs: 300,
      startStaggerMs: 16,
      latencyMs: 150,
      videoLatencyMs: 0,
      videoLandBehindAimSec: null,
      videoLandBehindStemsSec: null,
    });

    await expect
      .poll(async () => settled(await metrics(page)), { timeout: 5000, intervals: [100] })
      .toBe(true);

    // --- User seek through the real transport slider (+1 s step) ---
    const seekSlider = page.locator('[aria-label="Seek"] [role="slider"]');
    await seekSlider.focus();
    const seekedAt = await page.evaluate(() => performance.now());
    await page.keyboard.press('ArrowRight');
    await pollSettledRelativeTo(page, seekedAt, SETTLE_DEADLINE_MS);

    // --- Pause, then resume ---
    await expect
      .poll(async () => settled(await metrics(page)), { timeout: 5000, intervals: [100] })
      .toBe(true);
    const atPause = await metrics(page);
    const seeksAtPause = await probeCount(page, 'stemSeeks');

    // The transport auto-hides its pointer events while playing and idle;
    // a mouse move over the player re-arms the 2.5 s visibility window.
    await page.mouse.move(200, 200);
    await page.mouse.move(600, 400);
    await page.getByRole('button', { name: 'Pause' }).click();
    await expect(page.getByRole('button', { name: 'Play' })).toBeVisible({ timeout: 5_000 });
    await page.waitForTimeout(1000);
    const resumedAt = await page.evaluate(() => performance.now());
    await page.getByRole('button', { name: 'Play' }).click();
    await expect(page.getByRole('button', { name: 'Pause' })).toBeVisible({ timeout: 5_000 });

    // Back in sync within 3 s of the resume, and the pause/resume itself
    // never seeked a stem: the resume stagger is absorbed by delays plus a
    // move of the video, exactly like the start stagger.
    await pollSettledRelativeTo(page, resumedAt, SETTLE_DEADLINE_MS);
    const m = await metrics(page);
    expect(spreadMs(m)).toBeLessThanOrEqual(10);
    STEM_IDS.forEach((id) => {
      expect(m.stems[id].hardSeeks, `${id} hardSeeks across pause/resume`).toBe(
        atPause.stems[id].hardSeeks,
      );
    });
    expect(await probeCount(page, 'stemSeeks')).toBe(seeksAtPause);
    expect(await probeCount(page, 'rateChanges')).toBe(0);
    expect((await videoState(page)).ended).toBe(false);
  });
});
