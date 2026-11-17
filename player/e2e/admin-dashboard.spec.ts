import { test, expect, type Page, type Route } from '@playwright/test';

// Fully mocked UI checks should fail in seconds, not inherit the 15-minute
// media-playback timeout from the shared Playwright config.
test.use({ expect: { timeout: 5000 }, timeout: 60_000 });

// Administrator page (/admin) against fully offline page.route() mocks, in
// the style of auth-recovery.spec.ts / library-scroll.spec.ts. The real
// /api/admin routes live behind the owner's Tailscale-only proxy (they 404
// from the public listener — invariant G1), so these tests mock exactly the
// payloads the page renders: jobs with failure detail and discrepancy flags,
// the job-detail trace with its evidence checklist, node use switches, and
// the routing/RunPod-limit controls.

const FAILED_ID = 'e2eadminfailedjob00000000000000001';
const READY_ID = 'e2eadmintreadyjob000000000000000001';
const NOW = () => new Date().toISOString();
const minutesAgo = (m: number) => new Date(Date.now() - m * 60_000).toISOString();

interface Captured {
  puts: Array<Record<string, unknown>>;
  nodePosts: string[];
  statusQueries: string[];
  s3Queries: string[];
}

const failedJob = () => ({
  jobId: FAILED_ID,
  status: 'failed',
  stage: 'failed',
  sourceType: 'url',
  title: 'Broken Ballad',
  artist: 'E2E',
  attempt: 2,
  executor: 'runpod',
  runpodJobId: 'rp-77',
  workerPhase: 'failed',
  workerHeartbeatAt: minutesAgo(12),
  errorCode: 'RUNPOD_DISPATCH_FAILED',
  errorDetail: 'RunPod job rp-77 FAILED: cuda out of memory on worker gpu-3',
  leaseOwner: null,
  leaseExpiresAt: null,
  nextRetryAt: null,
  trackId: null,
  createdAt: minutesAgo(30),
  updatedAt: minutesAgo(12),
  flags: ['assignment_stalled', 'open_assignment_on_terminal_job'],
});

const readyJob = () => ({
  jobId: READY_ID,
  status: 'ready',
  stage: 'ready',
  sourceType: 'upload',
  title: 'Golden Take',
  artist: 'E2E',
  attempt: 0,
  executor: 'node:mac-mini',
  runpodJobId: null,
  workerPhase: null,
  workerHeartbeatAt: null,
  errorCode: null,
  errorDetail: null,
  leaseOwner: null,
  leaseExpiresAt: null,
  nextRetryAt: null,
  trackId: 'e2e-track-id-000000000000000000000001',
  createdAt: minutesAgo(90),
  updatedAt: minutesAgo(80),
  flags: [],
});

const nodesPayload = () => ({
  nodes: [
    {
      id: 'mac-mini',
      displayName: 'Mac mini',
      online: true,
      enabled: true,
      capabilities: { download: true, separate: true, device: 'mps' },
      agentVersion: '0.4.0',
      lastSeenAt: minutesAgo(0.2),
      busyAssignmentId: 'e2e-assignment-000000000000001',
      currentAssignment: {
        id: 'e2e-assignment-000000000000001',
        jobId: FAILED_ID,
        kind: 'separate',
        status: 'running',
        phase: 'separate: 80%',
        heartbeatAt: minutesAgo(14),
        createdAt: minutesAgo(20),
      },
    },
    {
      id: 'dell-9530',
      displayName: 'Dell 9530',
      online: false,
      enabled: false,
      capabilities: { separate: true, device: 'cuda' },
      agentVersion: '0.4.0',
      lastSeenAt: minutesAgo(240),
      busyAssignmentId: null,
      currentAssignment: null,
    },
  ],
  onlineSeconds: 45,
});

const settingsPayload = () => ({
  nodeRouting: { value: 'prefer', source: 'environment', error: null },
  runpodMaxConcurrent: { value: 2, source: 'database', error: null },
  runpodConfigured: true,
  jobsAtRunpod: 1,
});

const detailPayload = (withS3: boolean) => ({
  job: {
    ...failedJob(),
    idempotencyKey: `${FAILED_ID}:2`,
    profileVersion: 1,
    inputChecksum: null,
    outputChecksums: null,
    stageTimings: { downloading: 4.2 },
  },
  derivedTrackId: 'e2e-track-id-000000000000000000000001',
  dispatchAttempt: {
    idempotencyKey: `${FAILED_ID}:2`,
    attemptPrefix: `tracks/e2e-track-id/1/separation/attempts/${FAILED_ID}`,
    runpodJobId: 'rp-77',
  },
  events: [
    { id: 1, event: 'created', detail: { source_type: 'url' }, createdAt: minutesAgo(30) },
    {
      id: 2,
      event: 'stage_completed',
      detail: { from: 'pending', to: 'downloading', duration_s: 0.1 },
      createdAt: minutesAgo(29),
    },
    {
      id: 3,
      event: 'runpod_dispatch_started',
      detail: { idempotency_key: `${FAILED_ID}:2`, attempt: 2 },
      createdAt: minutesAgo(28),
    },
    {
      id: 4,
      event: 'failed',
      detail: { stage: 'dispatched', error_code: 'RUNPOD_DISPATCH_FAILED' },
      createdAt: minutesAgo(12),
    },
  ],
  assignments: [
    {
      id: 'e2e-assignment-000000000000001',
      nodeId: 'mac-mini',
      kind: 'separate',
      status: 'running',
      idempotencyKey: `${FAILED_ID}:1`,
      phase: 'separate: 80%',
      heartbeatAt: minutesAgo(14),
      detail: null,
      createdAt: minutesAgo(20),
      updatedAt: minutesAgo(14),
    },
  ],
  track: null,
  evidence: [
    {
      stage: 'pending',
      name: 'created_event',
      expected: 'a created event records ingest',
      observed: 'present',
      proof: 'job_events',
      checkedAt: NOW(),
      status: 'ok',
    },
    {
      stage: 'dispatched',
      name: 'assignment_stall',
      expected: 'a heartbeat within 300s',
      observed: 'no heartbeat for 840s',
      proof: 'node_assignments.heartbeat_at',
      checkedAt: NOW(),
      status: 'mismatch',
    },
    ...(withS3
      ? [
          {
            stage: 'downloading',
            name: 'source',
            expected: 'sources/{track}/source.mp4 exists',
            observed: 'absent',
            proof: 'S3 HEAD sources/e2e-track-id/source.mp4',
            checkedAt: NOW(),
            status: 'missing',
          },
          {
            stage: 'downloading',
            name: 'source_receipt',
            expected: 'sources/{track}/source.json (A12)',
            observed: 'S3 check failed: bucket unreachable',
            proof: 'S3 HEAD sources/e2e-track-id/source.json',
            checkedAt: NOW(),
            status: 'unknown',
          },
        ]
      : []),
  ],
});

function routeAdminApi(page: Page, captured: Captured) {
  // Unrelated background polling never reaches the admin page, but keep the
  // generic stub first-registered so specific handlers win (Playwright tries
  // the most recently registered handler first).
  void page.route('**/api/**', (route) => route.fulfill({ json: {} }));

  // Job list: /api/admin/jobs with or without a ?status= query.
  void page.route('**/api/admin/jobs*', (route: Route) => {
    const url = new URL(route.request().url());
    const status = url.searchParams.get('status');
    captured.statusQueries.push(status ?? '(none)');
    const jobs = status === 'ready' ? [readyJob()] : [failedJob(), readyJob()];
    return route.fulfill({
      json: { jobs, total: jobs.length, limit: 50, offset: 0, counts: { failed: 1, ready: 1 } },
    });
  });

  // Job detail (registered after the list route so /jobs/{id} wins).
  void page.route('**/api/admin/jobs/*', async (route: Route) => {
    const url = new URL(route.request().url());
    const withS3 = url.searchParams.get('verify') === 's3';
    if (withS3) captured.s3Queries.push(url.search);
    return route.fulfill({ json: detailPayload(withS3) });
  });

  void page.route('**/api/admin/nodes', (route) =>
    route.fulfill({ json: nodesPayload() })
  );

  void page.route('**/api/admin/settings', async (route: Route) => {
    if (route.request().method() === 'PUT') {
      captured.puts.push(route.request().postDataJSON() as Record<string, unknown>);
    }
    return route.fulfill({ json: settingsPayload() });
  });

  void page.route('**/api/admin/nodes/*/disable', (route: Route) => {
    captured.nodePosts.push(route.request().url());
    return route.fulfill({ json: { ok: true, enabled: false } });
  });
  void page.route('**/api/admin/nodes/*/enable', (route: Route) =>
    route.fulfill({ json: { ok: true, enabled: true } })
  );
}

test.describe('Admin page (/admin)', () => {
  test('lists failed jobs first with error code, full error text and flag badges', async ({
    page,
  }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    await page.goto('/admin');

    // Routing summary and RunPod state render.
    await expect(page.getByText('1 at RunPod now')).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('RunPod configured')).toBeVisible();

    // The failed job card carries the structured code, the full error text,
    // and its discrepancy flags as badges.
    const failedCard = page.locator('button', { hasText: 'Broken Ballad' });
    await expect(failedCard).toBeVisible();
    await expect(failedCard.getByText('RUNPOD_DISPATCH_FAILED', { exact: false })).toBeVisible();
    await expect(
      failedCard.getByText('cuda out of memory on worker gpu-3', { exact: false })
    ).toBeVisible();
    await expect(failedCard.getByText('assignment stalled')).toBeVisible();
    await expect(failedCard.getByText('open assignment on terminal job')).toBeVisible();

    // Failed sorts before ready.
    const cards = page.locator('section[aria-label="Jobs"] button');
    await expect(cards.first()).toContainText('Broken Ballad');

    // The status filter asks the API and the view follows.
    await page.getByLabel('Filter').selectOption('ready');
    await expect
      .poll(() => captured.statusQueries[captured.statusQueries.length - 1])
      .toBe('ready');
    await expect(page.locator('button', { hasText: 'Broken Ballad' })).toHaveCount(0);
    await expect(page.locator('button', { hasText: 'Golden Take' })).toBeVisible();
  });

  test('job detail shows the timeline, assignments and evidence, and Check storage calls verify=s3', async ({
    page,
  }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    await page.goto('/admin');

    await page.locator('button', { hasText: 'Broken Ballad' }).click();
    const detail = page.locator('section[aria-label="Job detail"]');
    await expect(detail).toBeVisible({ timeout: 10_000 });

    // Timeline with wall-clock gaps and event details.
    await expect(detail.getByText('runpod_dispatch_started')).toBeVisible();
    await expect(detail.getByText('+1m').first()).toBeVisible();

    // The assignment with its node, status and stale heartbeat.
    await expect(detail.getByText('mac-mini')).toBeVisible();
    await expect(detail.getByText('separate: 80%')).toBeVisible();

    // Evidence checklist renders statuses; the S3 items are absent until
    // "Check storage" asks for them.
    await expect(detail.getByText('created_event')).toBeVisible();
    await expect(detail.getByText('no heartbeat for 840s')).toBeVisible();
    await expect(detail.getByText('source_receipt')).toHaveCount(0);

    await detail.getByRole('button', { name: 'Check storage' }).click();
    await expect
      .poll(() => captured.s3Queries.length)
      .toBeGreaterThan(0);
    await expect(detail.getByText('with storage checks')).toBeVisible();
    await expect(detail.getByText('source_receipt')).toBeVisible();
    await expect(detail.getByText('S3 check failed: bucket unreachable')).toBeVisible();
  });

  test('routing controls PUT the mode and the RunPod limit', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    await page.goto('/admin');

    await expect(page.getByText('Max performance')).toBeVisible({ timeout: 10_000 });
    await page.getByText('Max performance').click();
    await expect
      .poll(() => captured.puts.find((put) => put.nodeRouting === 'download_only'))
      .toBeTruthy();

    await page.getByRole('button', { name: 'Paused' }).click();
    await expect
      .poll(() => captured.puts.find((put) => put.runpodMaxConcurrent === 0))
      .toBeTruthy();

    // Node use switch posts disable (click toggles the checkbox).
    await page.getByLabel('Stop using Mac mini').click();
    await expect.poll(() => captured.nodePosts.length).toBe(1);
    expect(captured.nodePosts[0]).toContain('/api/admin/nodes/mac-mini/disable');
  });

  test('ignores a slower detail response after the selected job changes', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    let releaseSlow: (() => void) | undefined;
    const slow = new Promise<void>((resolve) => { releaseSlow = resolve; });
    await page.route(`**/api/admin/jobs/${FAILED_ID}*`, async (route) => {
      await slow;
      await route.fulfill({ json: detailPayload(false) });
    });
    await page.route(`**/api/admin/jobs/${READY_ID}*`, (route) =>
      route.fulfill({
        json: {
          ...detailPayload(false),
          job: {
            ...readyJob(),
            idempotencyKey: `${READY_ID}:0`,
            profileVersion: 1,
            inputChecksum: null,
            outputChecksums: null,
            stageTimings: {},
          },
          derivedTrackId: READY_ID,
          dispatchAttempt: { idempotencyKey: `${READY_ID}:0`, attemptPrefix: null, runpodJobId: null },
          events: [{ id: 99, event: 'ready_job_selected', detail: {}, createdAt: NOW() }],
          assignments: [],
          evidence: [],
        },
      })
    );
    await page.goto('/admin');
    await page.locator('button', { hasText: 'Broken Ballad' }).click();
    await page.locator('button', { hasText: 'Golden Take' }).click();
    const detail = page.locator('section[aria-label="Job detail"]');
    await expect(detail.getByText('ready_job_selected')).toBeVisible();
    releaseSlow?.();
    await page.waitForTimeout(150);
    await expect(detail.getByText('ready_job_selected')).toBeVisible();
    await expect(detail.getByText('runpod_dispatch_started')).toHaveCount(0);
  });

  test('preserves an unsaved RunPod limit across refreshes and saves that value', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    await page.goto('/admin');
    const input = page.getByLabel('RunPod concurrency limit');
    await expect(input).toHaveValue('2');
    await input.fill('7');
    const refreshed = page.waitForResponse(
      (response) => response.url().endsWith('/api/admin/settings') && response.request().method() === 'GET'
    );
    await page.getByTitle('Refresh').click();
    await refreshed;
    await expect(input).toHaveValue('7');
    await page.getByRole('button', { name: 'Set limit' }).click();
    await expect.poll(() => captured.puts.at(-1)).toEqual({ runpodMaxConcurrent: 7 });
  });

  test('navigates administrator job history with limit and offset', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    const offsets: number[] = [];
    await page.route('**/api/admin/jobs?*', (route) => {
      const url = new URL(route.request().url());
      const offset = Number(url.searchParams.get('offset') ?? '0');
      offsets.push(offset);
      return route.fulfill({
        json: {
          jobs: [offset === 0 ? failedJob() : readyJob()],
          total: 121,
          limit: 50,
          offset,
          counts: { failed: 1, ready: 1 },
        },
      });
    });
    await page.goto('/admin');
    await page.getByRole('button', { name: 'Next page' }).click();
    await expect.poll(() => offsets.at(-1)).toBe(50);
    await expect(page.locator('button', { hasText: 'Golden Take' })).toBeVisible();
    await page.getByRole('button', { name: 'Previous page' }).click();
    await expect.poll(() => offsets.at(-1)).toBe(0);
  });

  test('surfaces node toggle failures and keeps confirmed node state', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    await page.route('**/api/admin/nodes/mac-mini/disable', (route) =>
      route.fulfill({ status: 500, json: { detail: 'database is read-only' } })
    );
    await page.goto('/admin');
    const checkbox = page.getByLabel('Stop using Mac mini');
    await expect(checkbox).toBeChecked();
    await checkbox.click();
    await expect(page.getByText('database is read-only')).toBeVisible();
    await expect(page.getByLabel('Stop using Mac mini')).toBeChecked();
  });

  test('aborts a hung settings request at the refresh timeout', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    await page.route('**/api/admin/settings', async (route) => {
      await new Promise((resolve) => setTimeout(resolve, 30_000));
      await route.fulfill({ json: settingsPayload() }).catch(() => undefined);
    });
    await page.goto('/admin');
    // Jobs and nodes must render when the shared controller aborts settings
    // at 10 s, not wait for this route's 30 s response. The margin between
    // the abort and this deadline is wide on purpose: a 0.5 s margin failed
    // under full-suite load.
    await expect(page.locator('button', { hasText: 'Broken Ballad' })).toBeVisible({ timeout: 20_000 });
  });

  test('distinguishes an unknown job from an unavailable admin network', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    await page.route(`**/api/admin/jobs/${FAILED_ID}*`, (route) =>
      route.fulfill({ status: 404, json: { detail: 'Job not found' } })
    );
    await page.goto('/admin');
    await page.locator('button', { hasText: 'Broken Ballad' }).click();
    await expect(page.locator('section[aria-label="Job detail"]').getByText('Job not found')).toBeVisible();
    await expect(page.getByText('The admin API is not reachable from this network.')).toHaveCount(0);
  });

  test('offers splitting as an administrator status filter', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    routeAdminApi(page, captured);
    await page.goto('/admin');
    await expect(page.getByLabel('Filter').locator('option[value="splitting"]')).toHaveCount(1);
  });

  test('says so plainly when the admin API answers 404 off the Tailscale network', async ({
    page,
  }) => {
    await page.route('**/api/admin/**', (route) =>
      route.fulfill({ status: 404, json: { detail: 'Not Found' } })
    );
    await page.goto('/admin');
    await expect(
      page.getByText('The admin API is not reachable from this network.')
    ).toBeVisible({ timeout: 10_000 });
    await expect(page.getByText('Connect to Tailscale and refresh.')).toBeVisible();
  });

  test('renders a null dispatch identity without a page error', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    routeAdminApi(page, captured);
    await page.route('**/api/admin/jobs/*', (route) =>
      route.fulfill({
        json: {
          ...detailPayload(false),
          dispatchAttempt: { idempotencyKey: null, attemptPrefix: null, runpodJobId: null },
        },
      })
    );
    await page.goto('/admin');
    await page.locator('button', { hasText: 'Broken Ballad' }).click();

    const detail = page.locator('section[aria-label="Job detail"]');
    await expect(detail).toContainText('attempt key unknown / legacy');
    await expect(detail.getByText('runpod_dispatch_started')).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('renders a job with no events, assignments, track or evidence', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    routeAdminApi(page, captured);
    await page.route('**/api/admin/jobs/*', (route) =>
      route.fulfill({
        json: { ...detailPayload(false), events: [], assignments: [], track: null, evidence: [] },
      })
    );
    await page.goto('/admin');
    await page.locator('button', { hasText: 'Broken Ballad' }).click();

    const detail = page.locator('section[aria-label="Job detail"]');
    await expect(detail.getByText('No job events recorded.')).toBeVisible();
    await expect(detail.getByText('No node assignments.')).toBeVisible();
    await expect(detail.getByText('No track row.')).toBeVisible();
    await expect(detail.getByText('No evidence checks recorded.')).toBeVisible();
    expect(pageErrors).toEqual([]);
  });

  test('renders an evidence item of every status', async ({ page }) => {
    const captured: Captured = { puts: [], nodePosts: [], statusQueries: [], s3Queries: [] };
    const pageErrors: string[] = [];
    page.on('pageerror', (error) => pageErrors.push(error.message));
    routeAdminApi(page, captured);
    const statuses = ['ok', 'missing', 'mismatch', 'not_yet', 'unknown'] as const;
    await page.route('**/api/admin/jobs/*', (route) =>
      route.fulfill({
        json: {
          ...detailPayload(false),
          evidence: statuses.map((status) => ({
            stage: 'dispatched',
            name: `evidence_${status}`,
            expected: `expected ${status}`,
            observed: `observed ${status}`,
            proof: 'mocked evidence',
            checkedAt: NOW(),
            status,
          })),
        },
      })
    );
    await page.goto('/admin');
    await page.locator('button', { hasText: 'Broken Ballad' }).click();

    const detail = page.locator('section[aria-label="Job detail"]');
    for (const status of statuses) {
      await expect(detail.getByText(`evidence_${status}`)).toBeVisible();
      await expect(detail.getByText(`dispatched · ${status}`, { exact: true })).toBeVisible();
    }
    expect(pageErrors).toEqual([]);
  });
});
