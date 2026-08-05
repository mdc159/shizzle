import { expect, test, type CDPSession, type Page, type TestInfo } from '@playwright/test';
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';

type StemState = {
  skewMs: number | null;
  paused: boolean;
  errorCode: number | null;
  gainLinear: number;
};

type PlaybackMetrics = {
  stems: Record<string, StemState>;
  desiredPlaying: boolean;
  health: {
    status: string;
    recoveryAttempts: number;
    recoverySuccesses: number;
    lastHealthyAtMs: number | null;
  };
  output: { rmsDbfs: number | null; peakDbfs: number | null; silentForMs: number };
  video: { currentTime: number; paused: boolean; errorCode: number | null } | null;
  incidents: Array<{ sequence: number; code: string; detail: string; atMs: number }>;
};

type LibraryTrack = { id: string; title: string; duration: number; generation: number };

declare global {
  interface Window {
    __shizzlePlaybackHealth: { getMetrics(): PlaybackMetrics };
    __shizzleTestAudios: HTMLAudioElement[];
  }
}

const enabled = process.env.SHIZZLE_PRODUCTION_PLAYBACK === '1';
const passcode = process.env.SHIZZLE_E2E_PASSCODE;
const selectedTrackId = process.env.SHIZZLE_TRACK_ID;
const trackLimit = Number(process.env.SHIZZLE_TRACK_LIMIT || 0);
const repeatCount = Math.max(1, Number(process.env.SHIZZLE_REPEAT_COUNT || 1));
const mode = process.env.SHIZZLE_PLAYBACK_MODE || 'stress';
const seed = Number(process.env.SHIZZLE_SEEK_SEED || 0x5a17_2026);

function seededFractions(count: number, initialSeed: number): number[] {
  let state = initialSeed >>> 0;
  return Array.from({ length: count }, () => {
    state ^= state << 13;
    state ^= state >>> 17;
    state ^= state << 5;
    return 0.03 + ((state >>> 0) / 0xffff_ffff) * 0.94;
  });
}

async function metrics(page: Page): Promise<PlaybackMetrics> {
  return page.evaluate(() => window.__shizzlePlaybackHealth.getMetrics());
}

async function installDirectStemFaultHook(page: Page): Promise<void> {
  await page.addInitScript(() => {
    const NativeAudio = window.Audio;
    window.__shizzleTestAudios = [];
    function TrackedAudio(src?: string): HTMLAudioElement {
      const element = new NativeAudio(src);
      window.__shizzleTestAudios.push(element);
      if (window.__shizzleTestAudios.length > 6) {
        window.__shizzleTestAudios.splice(0, window.__shizzleTestAudios.length - 6);
      }
      return element;
    }
    TrackedAudio.prototype = NativeAudio.prototype;
    Object.setPrototypeOf(TrackedAudio, NativeAudio);
    window.Audio = TrackedAudio as typeof Audio;
  });
}

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

async function getLibrary(page: Page): Promise<LibraryTrack[]> {
  return page.evaluate(async () => {
    const token = localStorage.getItem('shizzle_token');
    const response = await fetch('/api/library', {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      credentials: 'include',
    });
    if (!response.ok) throw new Error(`library HTTP ${response.status}`);
    const body = (await response.json()) as { tracks: LibraryTrack[] };
    return body.tracks;
  });
}

async function loadTrack(page: Page, track: LibraryTrack): Promise<Record<string, unknown>> {
  await page.keyboard.press('Escape');
  await page.mouse.move(20, 20);
  await page.getByRole('button', { name: 'Library' }).click();
  const heading = page.getByRole('heading', { name: track.title, exact: true });
  await expect(heading).toBeAttached({ timeout: 30_000 });
  // Radix's drawer can report lower cards outside the viewport even after an
  // automatic scroll. Native click still exercises the React selection path.
  await heading.evaluate((element) => {
    const card = element.closest<HTMLElement>('.cursor-pointer');
    if (!card) throw new Error('library card not found');
    card.click();
  });
  await expect(page.getByText(track.title, { exact: true }).first()).toBeVisible({
    timeout: 30_000,
  });
  await page.waitForFunction(() => window.__shizzleTestAudios.length === 6, undefined, {
    timeout: 30_000,
  });
  await page.waitForFunction(
    () => Object.keys(window.__shizzlePlaybackHealth.getMetrics().stems).length === 6,
    undefined,
    { timeout: 30_000 },
  );
  await expect(page.getByRole('button', { name: 'Play' })).toBeEnabled({ timeout: 30_000 });
  const initial = await metrics(page);
  expect(initial.incidents).toEqual([]);
  expect(initial.health.recoveryAttempts).toBe(0);

  return page.evaluate(async (trackId) => {
    const token = localStorage.getItem('shizzle_token');
    const response = await fetch(`/api/tracks/${encodeURIComponent(trackId)}/manifest`, {
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      credentials: 'include',
    });
    if (!response.ok) throw new Error(`manifest HTTP ${response.status}`);
    return (await response.json()) as Record<string, unknown>;
  }, track.id);
}

function evidenceSafeUrl(value: unknown): unknown {
  if (typeof value !== 'string' || !/^https?:\/\//i.test(value)) return value;
  const parsed = new URL(value);
  return `${parsed.origin}${parsed.pathname}${parsed.search ? '?signed=redacted' : ''}`;
}

function evidenceSafeManifest(manifest: Record<string, unknown>): Record<string, unknown> {
  const safe = structuredClone(manifest);
  safe.video = evidenceSafeUrl(safe.video);
  if (Array.isArray(safe.stems)) {
    safe.stems = safe.stems.map((stem) => {
      if (!stem || typeof stem !== 'object') return stem;
      const copy = { ...(stem as Record<string, unknown>) };
      copy.file = evidenceSafeUrl(copy.file);
      return copy;
    });
  }
  return safe;
}

async function captureVideoStaging(page: Page): Promise<Record<string, unknown>> {
  const staging = await page.evaluate(() => {
    const video = document.querySelector('video');
    const resource = performance
      .getEntriesByType('resource')
      .filter((entry): entry is PerformanceResourceTiming => entry instanceof PerformanceResourceTiming)
      .filter((entry) => entry.initiatorType === 'fetch' && entry.name.includes('/video.mp4'))
      .at(-1);
    return {
      sourceScheme: video?.currentSrc.split(':', 1)[0] ?? null,
      stagedBytes: Number(video?.dataset.stagedBytes ?? 0),
      readyState: video?.readyState ?? null,
      duration: video?.duration ?? null,
      resource: resource
        ? {
            name: resource.name,
            durationMs: resource.duration,
            transferSize: resource.transferSize,
            encodedBodySize: resource.encodedBodySize,
            decodedBodySize: resource.decodedBodySize,
            nextHopProtocol: resource.nextHopProtocol,
            responseStatus: resource.responseStatus,
          }
        : null,
    };
  });
  if (staging.resource) staging.resource.name = evidenceSafeUrl(staging.resource.name) as string;
  expect(staging.sourceScheme).toBe('blob');
  expect(staging.stagedBytes).toBeGreaterThan(0);
  expect(staging.readyState).toBeGreaterThanOrEqual(1);
  return staging;
}

async function waitHealthy(
  page: Page,
  timeout = 3_000,
  afterHealthyAtMs: number | null = null,
): Promise<PlaybackMetrics> {
  const healthyState = await page.waitForFunction(
    (afterHealthyAt) => {
      const state = window.__shizzlePlaybackHealth.getMetrics();
      const stems = Object.values(state.stems);
      const skews = stems
        .map((stem) => stem.skewMs)
        .filter((value): value is number => typeof value === 'number');
      const healthy =
        state.health.status === 'healthy' &&
        (afterHealthyAt === null || (state.health.lastHealthyAtMs ?? 0) > afterHealthyAt) &&
        state.video?.paused === false &&
        state.video.errorCode === null &&
        state.output.rmsDbfs !== null &&
        stems.length === 6 &&
        stems.every((stem) => !stem.paused && stem.errorCode === null) &&
        Math.max(...skews) - Math.min(...skews) <= 40 &&
        Math.max(...skews.map(Math.abs)) <= 40;
      return healthy ? state : null;
    },
    afterHealthyAtMs,
    { timeout },
  );
  try {
    return (await healthyState.jsonValue()) as PlaybackMetrics;
  } finally {
    await healthyState.dispose();
  }
}

async function start(page: Page): Promise<void> {
  await page.getByRole('button', { name: 'Play' }).click();
  try {
    await waitHealthy(page, 15_000);
  } catch (error) {
    const state = await metrics(page);
    throw new Error(`playback did not become healthy: ${JSON.stringify(state)}`, {
      cause: error,
    });
  }
}

function assertBoundedRecoveries(state: PlaybackMetrics): void {
  const incidents = state.incidents.toSorted((a, b) => a.sequence - b.sequence);
  for (const [index, incident] of incidents.entries()) {
    if (incident.code !== 'recovery-started') continue;
    const outcome = incidents.slice(index + 1).find(
      (candidate) =>
        candidate.code === 'recovery-succeeded' || candidate.code === 'recovery-failed',
    );
    expect(outcome, `recovery outcome after incident ${incident.sequence}`).toBeDefined();
    expect(outcome?.code, `recovery result after incident ${incident.sequence}`).toBe(
      'recovery-succeeded',
    );
    expect(
      (outcome?.atMs ?? Number.POSITIVE_INFINITY) - incident.atMs,
      `recovery latency after incident ${incident.sequence}`,
    ).toBeLessThanOrEqual(3_000);
  }
}

async function stressSeeks(page: Page): Promise<Record<string, unknown>> {
  const seek = page.locator('[aria-label="Seek"]');
  const latencies: number[] = [];
  const observerLags: number[] = [];
  let maxInterStemSkewMs = 0;
  let maxStemVideoOffsetMs = 0;
  for (const fraction of seededFractions(20, seed)) {
    await page.mouse.move(20, 20);
    const box = await seek.boundingBox();
    if (!box) throw new Error('seek control is unavailable');
    const beforeSeek = await metrics(page);
    const startedAt = Date.now();
    await page.mouse.click(box.x + box.width * fraction, box.y + box.height / 2);
    let state: PlaybackMetrics;
    try {
      state = await waitHealthy(
        page,
        3_000,
        Math.max(beforeSeek.health.lastHealthyAtMs ?? 0, startedAt),
      );
    } catch (error) {
      const diagnostic = await metrics(page);
      throw new Error(
        `seek fraction ${fraction.toFixed(6)} did not settle: ${JSON.stringify(diagnostic)}`,
        { cause: error },
      );
    }
    const observedAt = Date.now();
    const completedAt = state.health.lastHealthyAtMs ?? observedAt;
    expect(completedAt, `direct healthy timestamp after seek fraction ${fraction}`).toBeGreaterThanOrEqual(startedAt);
    const latency = completedAt - startedAt;
    observerLags.push(Math.max(0, observedAt - completedAt));
    latencies.push(latency);
    const skews = Object.values(state.stems)
      .map((stem) => stem.skewMs)
      .filter((value): value is number => typeof value === 'number');
    maxInterStemSkewMs = Math.max(
      maxInterStemSkewMs,
      Math.max(...skews) - Math.min(...skews),
    );
    maxStemVideoOffsetMs = Math.max(maxStemVideoOffsetMs, ...skews.map(Math.abs));
    expect(latency, `seek fraction ${fraction}`).toBeLessThanOrEqual(3_000);
    expect(
      Math.max(...skews) - Math.min(...skews),
      `inter-stem skew after seek fraction ${fraction}`,
    ).toBeLessThanOrEqual(50);
    expect(
      Math.max(...skews.map(Math.abs)),
      `stem/video offset after seek fraction ${fraction}`,
    ).toBeLessThanOrEqual(50);
  }
  return {
    seed,
    count: latencies.length,
    p50Ms: latencies.toSorted((a, b) => a - b)[Math.floor(latencies.length * 0.5)],
    p95Ms: latencies.toSorted((a, b) => a - b)[Math.floor(latencies.length * 0.95)],
    maxMs: Math.max(...latencies),
    maxObserverLagMs: Math.max(...observerLags),
    maxInterStemSkewMs,
    maxStemVideoOffsetMs,
  };
}

async function stressTransport(page: Page): Promise<void> {
  for (let index = 0; index < 12; index += 1) {
    await page.mouse.move(20 + index, 20);
    await page.getByRole('button', { name: 'Pause' }).click();
    await page.waitForTimeout(75);
    await page.getByRole('button', { name: 'Play' }).click();
    await page.waitForTimeout(125);
  }
  await waitHealthy(page);
}

async function stressMixer(page: Page): Promise<Record<string, unknown>> {
  await page.mouse.move(20, 20);
  await page.getByRole('button', { name: 'Mixer' }).click();
  await page.getByRole('button', { name: 'vocals mute' }).click();
  await expect.poll(async () => (await metrics(page)).stems.vocals.gainLinear).toBeLessThan(0.01);
  await page.getByRole('button', { name: 'vocals unmute' }).click();
  await page.getByRole('button', { name: 'drums solo' }).click();
  await page.getByRole('button', { name: 'Reset mixer' }).click();
  await expect
    .poll(async () => Object.values((await metrics(page)).stems).every((stem) => stem.gainLinear > 0.99))
    .toBe(true);
  const state = await waitHealthy(page);
  await page.keyboard.press('Escape');
  return { output: state.output, gains: Object.fromEntries(Object.entries(state.stems).map(([id, stem]) => [id, stem.gainLinear])) };
}

async function injectStoppedStem(page: Page): Promise<Record<string, unknown>> {
  await page.waitForTimeout(1_100);
  const before = await metrics(page);
  const startedAt = Date.now();
  await page.evaluate(() => window.__shizzleTestAudios[2].pause());
  const state = await waitHealthy(page);
  const latencyMs = Date.now() - startedAt;
  expect(state.health.recoveryAttempts).toBeGreaterThan(before.health.recoveryAttempts);
  expect(state.health.recoverySuccesses).toBeGreaterThan(before.health.recoverySuccesses);
  expect(latencyMs).toBeLessThanOrEqual(3_000);
  return {
    latencyMs,
    incidents: state.incidents.filter(
      (incident) => !before.incidents.some((prior) => prior.sequence === incident.sequence),
    ),
  };
}

async function seekToFraction(page: Page, fraction: number): Promise<void> {
  const seek = page.locator('[aria-label="Seek"]');
  const box = await seek.boundingBox();
  if (!box) throw new Error('seek control is unavailable');
  await page.mouse.click(box.x + box.width * fraction, box.y + box.height / 2);
}

async function injectRangeFault(page: Page): Promise<Record<string, unknown>> {
  let abortedRequest: { url: string; range: string } | null = null;
  const routeHandler = async (route: import('@playwright/test').Route): Promise<void> => {
    const request = route.request();
    const range = request.headers().range;
    const isMedia = /\.(?:m4a|mp4)(?:\?|$)/i.test(request.url()) || request.url().includes('/cdn/');
    if (!abortedRequest && range && isMedia) {
      abortedRequest = { url: request.url().replace(/\?.*$/, '?redacted'), range };
      await route.abort('connectionreset');
      return;
    }
    await route.continue();
  };

  await page.route('**/*', routeHandler);
  const before = await metrics(page);
  const startedAt = Date.now();
  try {
    await seekToFraction(page, before.video && before.video.currentTime > 30 ? 0.07 : 0.91);
    await expect.poll(() => abortedRequest, { timeout: 3_000 }).not.toBeNull();
  } finally {
    await page.unroute('**/*', routeHandler);
  }
  const state = await waitHealthy(
    page,
    3_000,
    Math.max(before.health.lastHealthyAtMs ?? 0, startedAt),
  );
  const latencyMs = (state.health.lastHealthyAtMs ?? Date.now()) - startedAt;
  expect(latencyMs).toBeLessThanOrEqual(3_000);
  expect(state.video?.errorCode).toBeNull();
  return { abortedRequest, latencyMs, finalHealth: state.health };
}

async function freezeAndRestore(page: Page, cdp: CDPSession): Promise<Record<string, unknown>> {
  const before = await metrics(page);
  await cdp.send('Page.setWebLifecycleState', { state: 'frozen' });
  await page.waitForTimeout(750);
  const restoredAt = Date.now();
  await cdp.send('Page.setWebLifecycleState', { state: 'active' });
  const state = await waitHealthy(page, 3_000);
  const latencyMs = Date.now() - restoredAt;
  expect(latencyMs).toBeLessThanOrEqual(3_000);
  return {
    frozenForMs: 750,
    latencyMs,
    recoveryAttemptsDelta: state.health.recoveryAttempts - before.health.recoveryAttempts,
    finalHealth: state.health,
  };
}

async function constrainedNetwork(page: Page, cdp: CDPSession): Promise<Record<string, unknown>> {
  const profile = {
    latencyMs: 150,
    downloadBitsPerSecond: 4_000_000,
    uploadBitsPerSecond: 1_000_000,
  };
  await cdp.send('Network.enable');
  await cdp.send('Network.emulateNetworkConditions', {
    offline: false,
    latency: profile.latencyMs,
    downloadThroughput: profile.downloadBitsPerSecond / 8,
    uploadThroughput: profile.uploadBitsPerSecond / 8,
    connectionType: 'cellular4g',
  });
  const before = await metrics(page);
  const startedAt = Date.now();
  let constrainedState: PlaybackMetrics;
  let latencyMs: number;
  try {
    await seekToFraction(page, before.video && before.video.currentTime > 30 ? 0.83 : 0.17);
    constrainedState = await waitHealthy(
      page,
      3_000,
      Math.max(before.health.lastHealthyAtMs ?? 0, startedAt),
    );
    latencyMs = (constrainedState.health.lastHealthyAtMs ?? Date.now()) - startedAt;
    expect(latencyMs).toBeLessThanOrEqual(3_000);
  } finally {
    await cdp.send('Network.emulateNetworkConditions', {
      offline: false,
      latency: 0,
      downloadThroughput: -1,
      uploadThroughput: -1,
      connectionType: 'none',
    });
  }
  const restoredState = await waitHealthy(page, 3_000);
  return {
    profile,
    latencyMs,
    constrainedHealth: constrainedState.health,
    postRestoreHealth: restoredState.health,
  };
}

async function stressFaults(page: Page): Promise<Record<string, unknown>> {
  const cdp = await page.context().newCDPSession(page);
  try {
    return {
      rangeRequest: await injectRangeFault(page),
      backgroundForeground: await freezeAndRestore(page, cdp),
      constrainedNetwork: await constrainedNetwork(page, cdp),
    };
  } finally {
    await cdp.detach();
  }
}

async function naturalEndAndReplay(
  page: Page,
  track: LibraryTrack,
): Promise<Array<Record<string, unknown>>> {
  const video = page.locator('video');
  await expect(video).toHaveJSProperty('currentTime', 0);
  await start(page);
  const playthroughs: Array<Record<string, unknown>> = [];
  for (let repetition = 1; repetition <= repeatCount; repetition += 1) {
    const startedAt = new Date().toISOString();
    await page.waitForFunction(() => document.querySelector('video')?.ended, undefined, {
      timeout: (track.duration + 60) * 1000,
    });
    const ended = await metrics(page);
    expect(ended.video?.errorCode).toBeNull();
    expect(ended.video?.currentTime ?? 0).toBeGreaterThanOrEqual(track.duration - 0.1);
    expect(Object.values(ended.stems).every((stem) => stem.errorCode === null)).toBe(true);
    expect(ended.incidents.some((incident) => incident.code === 'recovery-failed')).toBe(false);
    expect(
      ended.incidents.some(
        (incident) =>
          incident.code === 'video-media-error' || incident.code === 'stem-media-error',
      ),
    ).toBe(false);
    assertBoundedRecoveries(ended);
    await page.mouse.move(20, 20);
    const replayStartedAtMs = Date.now();
    await page.getByRole('button', { name: 'Play' }).click();
    const replay = await waitHealthy(page, 3_000);
    const replayLatencyMs = Date.now() - replayStartedAtMs;
    expect(replayLatencyMs).toBeLessThanOrEqual(3_000);
    expect(replay.video?.currentTime).toBeLessThanOrEqual(3);
    playthroughs.push({
      repetition,
      startedAt,
      endedAt: new Date().toISOString(),
      endedMetrics: ended,
      replayLatencyMs,
      replayMetrics: replay,
    });
  }
  return playthroughs;
}

async function writeConfiguredResult(result: Record<string, unknown>): Promise<void> {
  const body = `${JSON.stringify(result, null, 2)}\n`;
  const configuredPath = process.env.SHIZZLE_RESULT_PATH;
  if (configuredPath) {
    const outputPath = path.resolve(configuredPath);
    await mkdir(path.dirname(outputPath), { recursive: true });
    await writeFile(outputPath, body, 'utf8');
  }
}

async function attachResult(testInfo: TestInfo, result: Record<string, unknown>): Promise<void> {
  const body = `${JSON.stringify(result, null, 2)}\n`;
  await testInfo.attach('production-playback-result', {
    body: Buffer.from(body),
    contentType: 'application/json',
  });
  await writeConfiguredResult(result);
}

test('production cloud playback acceptance', async ({ page }, testInfo) => {
  test.setTimeout(
    mode === 'natural' || mode === 'playlist'
      ? 5 * 60 * 60 * 1000
      : 45 * 60 * 1000,
  );
  test.skip(!enabled, 'Set SHIZZLE_PRODUCTION_PLAYBACK=1 for the production acceptance run');
  await installDirectStemFaultHook(page);
  await authenticate(page);
  const library = await getLibrary(page);
  let tracks = selectedTrackId
    ? library.filter((track) => track.id === selectedTrackId)
    : library;
  if (trackLimit > 0) tracks = tracks.slice(0, trackLimit);
  expect(tracks.length, 'selected production tracks').toBeGreaterThan(0);

  const results: Array<Record<string, unknown>> = [];
  for (const track of tracks) {
    const result: Record<string, unknown> = {
      track,
      mode,
      build: await page.locator('script[type="module"][src]').getAttribute('src'),
      startedAt: new Date().toISOString(),
    };
    try {
      result.manifest = evidenceSafeManifest(await loadTrack(page, track));
      result.videoStaging = await captureVideoStaging(page);
      if (mode === 'natural' || mode === 'playlist') {
        result.playthroughs = await naturalEndAndReplay(page, track);
      } else if (mode === 'faults') {
        await start(page);
        result.faults = await stressFaults(page);
      } else {
        await start(page);
        result.seeks = await stressSeeks(page);
        await stressTransport(page);
        result.mixer = await stressMixer(page);
        result.stoppedStem = await injectStoppedStem(page);
      }
      result.finalMetrics = await waitHealthy(page, 3_000);
      result.status = 'passed';
      result.completedAt = new Date().toISOString();
    } catch (error) {
      result.status = 'failed';
      result.error = error instanceof Error ? error.message : String(error);
      result.finalMetrics = await metrics(page).catch(() => null);
      result.failedAt = new Date().toISOString();
      results.push(result);
      await writeConfiguredResult({ status: 'failed', seed, mode, tracks: results });
      throw error;
    }
    results.push(result);
    await writeConfiguredResult({ status: 'running', seed, mode, tracks: results });
  }
  await attachResult(testInfo, { status: 'complete', seed, mode, tracks: results });
});
