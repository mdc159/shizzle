import { defineConfig } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  timeout: 15 * 60 * 1000, // 15 min — processing + full track playback
  expect: { timeout: 15 * 60 * 1000 },
  use: {
    baseURL: 'http://localhost:5173',
    headless: false,
    video: 'off',
    launchOptions: {
      args: ['--autoplay-policy=no-user-gesture-required'],
    },
  },
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:5173',
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
