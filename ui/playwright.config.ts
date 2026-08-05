import { defineConfig } from '@playwright/test';

const productionBaseUrl = process.env.SHIZZLE_E2E_BASE_URL;

export default defineConfig({
  testDir: './e2e',
  timeout: 15 * 60 * 1000, // 15 min — processing + full track playback
  expect: { timeout: 15 * 60 * 1000 },
  use: {
    baseURL: productionBaseUrl || 'http://localhost:5173',
    headless: process.env.SHIZZLE_E2E_HEADLESS === '1',
    video: 'off',
    launchOptions: {
      args: [
        '--autoplay-policy=no-user-gesture-required',
        ...(process.env.SHIZZLE_DISABLE_QUIC === '1' ? ['--disable-quic'] : []),
      ],
    },
  },
  webServer: productionBaseUrl
    ? undefined
    : {
        command: 'npm run dev',
        url: 'http://localhost:5173',
        reuseExistingServer: true,
        timeout: 30_000,
      },
});
