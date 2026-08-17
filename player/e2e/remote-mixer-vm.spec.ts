import { test, expect, type Page } from '@playwright/test';

// Full-stack proof against a production-shaped deployment (the SHIZZLE-TEST
// VM or any real host): production player build served by Caddy, real
// passcode auth, a real track actually PLAYING, and a second browser context
// on /remote muting a stem — verified in the playing browser's measured
// post-gain PCM (signalRmsDbfs), not just its store.
//
// Run with:
//   SHIZZLE_E2E_BASE_URL=http://<host>  SHIZZLE_E2E_PASSCODE=<passcode>

const passcode = process.env.SHIZZLE_E2E_PASSCODE;

test.skip(
  !passcode || !process.env.SHIZZLE_E2E_BASE_URL,
  'needs SHIZZLE_E2E_BASE_URL and SHIZZLE_E2E_PASSCODE against a deployed stack'
);

const login = async (page: Page, path: string) => {
  await page.goto(path);
  await page.locator('input[type="password"]').fill(passcode!);
  await page.locator('button[type="submit"]').click();
};

interface StemProbe {
  gainLinear: number | null;
  rms: number | null;
  masterRms: number | null;
  health: string;
}

const probeVocals = (player: Page): Promise<StemProbe> =>
  player.evaluate(() => {
    const m = (window as unknown as {
      __shizzlePlaybackHealth: { getMetrics: () => {
        stems: Record<string, { gainLinear: number; signalRmsDbfs: number | null }>;
        output: { rmsDbfs: number | null };
        health: { status: string };
      } };
    }).__shizzlePlaybackHealth.getMetrics();
    return {
      gainLinear: m.stems.vocals ? m.stems.vocals.gainLinear : null,
      rms: m.stems.vocals ? m.stems.vocals.signalRmsDbfs : null,
      masterRms: m.output.rmsDbfs,
      health: m.health.status,
    };
  });

test('remote mute is audible in the playing browser (full stack)', async ({ browser }) => {
  test.setTimeout(240_000);

  // The VM stack signs its own certificate (Caddy internal CA).
  const playerCtx = await browser.newContext({ ignoreHTTPSErrors: true });
  const remoteCtx = await browser.newContext({ ignoreHTTPSErrors: true });
  const player = await playerCtx.newPage();
  const remote = await remoteCtx.newPage();

  // Player screen: log in, load the real track, press play.
  await login(player, '/');
  await player.getByRole('button', { name: 'Library' }).click();
  await player.getByText('Black Hole Sun', { exact: false }).first().click();
  const playButton = player.getByRole('button', { name: 'Play' });
  await expect(playButton).toBeEnabled({ timeout: 60_000 });
  await playButton.click();

  // Real audio must be flowing: engine healthy, master bus carrying signal,
  // vocals at unity with live post-gain PCM.
  await expect
    .poll(async () => (await probeVocals(player)).health, { timeout: 60_000 })
    .toBe('healthy');
  await expect
    .poll(async () => {
      const p = await probeVocals(player);
      return p.masterRms !== null && p.masterRms > -60;
    }, { timeout: 30_000 })
    .toBe(true);
  await expect
    .poll(async () => {
      const p = await probeVocals(player);
      return p.gainLinear !== null && p.gainLinear > 0.9 && p.rms !== null && p.rms > -70;
    }, { timeout: 30_000 })
    .toBe(true);

  // iPad stand-in: log in on /remote, confirm the relay connection.
  await login(remote, '/remote');
  await expect(remote.getByTestId('remote-connection')).toHaveText(/Connected/, {
    timeout: 15_000,
  });

  // Mute vocals from the remote → the playing browser's vocals channel goes
  // to zero gain AND its measured post-gain PCM falls silent, while the rest
  // of the band keeps playing on the master bus.
  await remote.getByRole('button', { name: 'vocals mute' }).click();
  await expect
    .poll(async () => (await probeVocals(player)).gainLinear, { timeout: 15_000 })
    .toBe(0);
  await expect
    .poll(async () => {
      const p = await probeVocals(player);
      return p.rms === null || p.rms < -70;
    }, { timeout: 20_000 })
    .toBe(true);
  await expect
    .poll(async () => {
      const p = await probeVocals(player);
      return p.masterRms !== null && p.masterRms > -60;
    }, { timeout: 15_000 })
    .toBe(true);

  // Unmute from the remote → vocals return, audibly.
  await remote.getByRole('button', { name: 'vocals unmute' }).click();
  await expect
    .poll(async () => {
      const p = await probeVocals(player);
      return p.gainLinear !== null && p.gainLinear > 0.9 && p.rms !== null && p.rms > -70;
    }, { timeout: 20_000 })
    .toBe(true);

  await playerCtx.close();
  await remoteCtx.close();
});
