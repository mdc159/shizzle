import { test, expect, type Page, type Route } from '@playwright/test';

// Regression test for issue #26: PlayerShell's manifest-fetch effect had no
// cancellation or request-identity guard, so a delayed response (success or
// failure) for a previously selected track could overwrite the currently
// selected track's manifest/duration/loading/error state. The fix aborts the
// in-flight fetch on effect cleanup and gates state updates with a `cancelled`
// flag scoped to that request, so only the latest active track request may
// ever call setManifest/setDuration/setIsLoading.
//
// Runs fully offline against the local dev server (same harness style as
// upload-metadata.spec.ts / manifest-trim.spec.ts): the control plane and two
// tracks' manifests are served from page.route(), each gated behind a
// per-request deferred promise the test resolves explicitly, so response
// ordering is deterministic regardless of real network timing.

type Track = {
  id: string;
  title: string;
  artist: string;
  slug: string;
  duration: number;
  publicUrl: string;
  status: 'ready';
};

const TRACKS: Record<'a' | 'b', Track> = {
  a: {
    id: 'e2e-race-a',
    title: 'Race Track A',
    artist: 'E2E',
    slug: 'e2e-race-a',
    duration: 100,
    publicUrl: '/tracks/e2e-race-a',
    status: 'ready',
  },
  b: {
    id: 'e2e-race-b',
    title: 'Race Track B',
    artist: 'E2E',
    slug: 'e2e-race-b',
    duration: 200,
    publicUrl: '/tracks/e2e-race-b',
    status: 'ready',
  },
};

function manifestFor(slug: 'a' | 'b') {
  const track = TRACKS[slug];
  return {
    track_id: track.slug,
    generation: 1,
    title: track.title,
    artist: track.artist,
    duration: track.duration,
    video: 'video.mp4',
    stems: [],
  };
}

interface Deferred {
  promise: Promise<void>;
  resolve: () => void;
}

function makeDeferred(): Deferred {
  let resolve!: () => void;
  const promise = new Promise<void>((r) => {
    resolve = r;
  });
  return { promise, resolve };
}

/**
 * Installs a gated manifest route. Every manifest request is queued (per
 * slug, in arrival order) behind a deferred the test controls explicitly via
 * `release(slug, requestIndex, outcome)`. `waitForRequest` resolves once the
 * Nth request for a slug has arrived, which is what lets the test issue the
 * next selection only after the current one is provably in flight.
 */
class ManifestGate {
  private queues: Record<string, Deferred[]> = { a: [], b: [] };
  private outcomes: Record<string, Array<'success' | 'fail'>> = { a: [], b: [] };
  private seen: Record<string, number> = { a: 0, b: 0 };

  async install(page: Page) {
    await page.route('**/api/tracks/*/manifest', async (route: Route) => {
      const match = route.request().url().match(/\/api\/tracks\/([^/]+)\/manifest/);
      const slug = match?.[1] === TRACKS.a.slug ? 'a' : match?.[1] === TRACKS.b.slug ? 'b' : null;
      if (!slug) {
        await route.fulfill({ status: 404, body: 'unknown track' });
        return;
      }
      const gate = makeDeferred();
      this.queues[slug].push(gate);
      this.seen[slug] += 1;
      await gate.promise;
      const idx = this.queues[slug].indexOf(gate);
      const outcome = this.outcomes[slug][idx] ?? 'success';
      try {
        if (outcome === 'fail') {
          await route.fulfill({ status: 500, body: 'manifest fetch failed' });
        } else {
          await route.fulfill({ json: manifestFor(slug) });
        }
      } catch {
        // The underlying request may already be aborted client-side (the
        // whole point of this test); Playwright can throw when fulfilling a
        // cancelled route. That is expected and not a test failure.
      }
    });
  }

  async waitForRequest(page: Page, slug: 'a' | 'b', count: number) {
    await expect.poll(() => this.seen[slug]).toBeGreaterThanOrEqual(count);
    void page; // kept for symmetry/readability with other wait helpers
  }

  /** Release the Nth (1-indexed) request for a slug with the given outcome. */
  release(slug: 'a' | 'b', requestNumber: number, outcome: 'success' | 'fail' = 'success') {
    const gate = this.queues[slug][requestNumber - 1];
    if (!gate) throw new Error(`No request #${requestNumber} recorded for slug ${slug}`);
    this.outcomes[slug][requestNumber - 1] = outcome;
    gate.resolve();
  }
}

type StoreState = {
  currentTrack: { slug: string } | null;
  manifest: { track_id?: string } | null;
  duration: number;
};

function getState(page: Page): Promise<StoreState> {
  return page.evaluate(
    () =>
      (window as unknown as { __shizzle: { store: { getState: () => StoreState } } }).__shizzle.store.getState(),
  );
}

async function setup(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('shizzle_token', 'e2e-token');
  });
  await page.route('**/api/media/session', (route) => route.fulfill({ json: { cloudfront: false } }));
  await page.route('**/api/library', (route) =>
    route.fulfill({ json: { tracks: [TRACKS.a, TRACKS.b], total: 2 } }),
  );

  const gate = new ManifestGate();
  await gate.install(page);

  await page.goto('/');
  await page.waitForFunction(() => !!(window as unknown as { __shizzle?: unknown }).__shizzle, undefined, {
    timeout: 10_000,
  });
  return gate;
}

async function selectTrack(page: Page, slug: 'a' | 'b') {
  await page.getByRole('button', { name: 'Library' }).click();
  const row = page.getByText(TRACKS[slug].title, { exact: true });
  await expect(row).toBeVisible({ timeout: 10_000 });
  await row.click();
}

test.describe('manifest request race (issue #26)', () => {
  test('A held, then B selected and succeeds, then stale A is released — B wins', async ({ page }) => {
    test.setTimeout(60_000);
    const gate = await setup(page);

    await selectTrack(page, 'a');
    await gate.waitForRequest(page, 'a', 1);

    await selectTrack(page, 'b');
    await gate.waitForRequest(page, 'b', 1);

    gate.release('b', 1, 'success');
    await expect.poll(async () => (await getState(page)).manifest?.track_id).toBe(TRACKS.b.slug);
    await expect.poll(async () => (await getState(page)).duration).toBe(TRACKS.b.duration);

    const stateBeforeStaleRelease = await getState(page);

    // Release the stale A response last. Whether it lands as a genuine
    // abort or a very-late fulfil, it must never win: B is the latest
    // active request.
    gate.release('a', 1, 'success');

    // Give any (incorrect) state update a chance to land before asserting
    // it did not.
    await page.waitForTimeout(500);

    const finalState = await getState(page);
    expect(finalState.currentTrack?.slug).toBe(TRACKS.b.slug);
    expect(finalState.manifest?.track_id).toBe(TRACKS.b.slug);
    expect(finalState.duration).toBe(TRACKS.b.duration);
    expect(finalState).toEqual(stateBeforeStaleRelease);

    await expect(page.getByText('Failed to load track stems')).toBeHidden();
  });

  test('A held, then B selected and succeeds, then stale A fails — B is unaffected', async ({ page }) => {
    test.setTimeout(60_000);
    const gate = await setup(page);

    await selectTrack(page, 'a');
    await gate.waitForRequest(page, 'a', 1);

    await selectTrack(page, 'b');
    await gate.waitForRequest(page, 'b', 1);

    gate.release('b', 1, 'success');
    await expect.poll(async () => (await getState(page)).manifest?.track_id).toBe(TRACKS.b.slug);

    // The stale A request fails after B has already succeeded. It must not
    // clear B's manifest or surface a user-visible error toast.
    gate.release('a', 1, 'fail');
    await page.waitForTimeout(500);

    const finalState = await getState(page);
    expect(finalState.currentTrack?.slug).toBe(TRACKS.b.slug);
    expect(finalState.manifest?.track_id).toBe(TRACKS.b.slug);
    expect(finalState.duration).toBe(TRACKS.b.duration);

    await expect(page.getByText('Failed to load track stems')).toBeHidden();
  });

  test('quick A -> B -> A settles on the second A request', async ({ page }) => {
    test.setTimeout(60_000);
    const gate = await setup(page);

    await selectTrack(page, 'a');
    await gate.waitForRequest(page, 'a', 1);

    await selectTrack(page, 'b');
    await gate.waitForRequest(page, 'b', 1);

    await selectTrack(page, 'a');
    await gate.waitForRequest(page, 'a', 2);

    // Resolve the second A request first — it is the only one that should
    // ever be allowed to set state.
    gate.release('a', 2, 'success');
    await expect.poll(async () => (await getState(page)).manifest?.track_id).toBe(TRACKS.a.slug);
    await expect.poll(async () => (await getState(page)).duration).toBe(TRACKS.a.duration);

    // Now release the two stale requests (first A, then B). Neither may
    // change the final state.
    gate.release('b', 1, 'success');
    gate.release('a', 1, 'success');
    await page.waitForTimeout(500);

    const finalState = await getState(page);
    expect(finalState.currentTrack?.slug).toBe(TRACKS.a.slug);
    expect(finalState.manifest?.track_id).toBe(TRACKS.a.slug);
    expect(finalState.duration).toBe(TRACKS.a.duration);
  });
});
