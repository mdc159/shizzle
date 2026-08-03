// Automated probe run: corrected >= 3 min, then raw ~1 min, on desktop Chrome.
// Requires: serve.py already running on PORT, global playwright install.
// Usage: node run-probe.mjs [port]
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { createRequire } from 'node:module';
import { execSync } from 'node:child_process';

// Resolve playwright from the global npm root (no local node_modules here).
const globalRoot = execSync('npm root -g').toString().trim();
const requireG = createRequire(join(globalRoot, 'x.js'));
const { chromium } = requireG('playwright');

const here = dirname(fileURLToPath(import.meta.url));
const outDir = join(here, 'results');
mkdirSync(outDir, { recursive: true });

const port = process.argv[2] || '8077';
const url = `http://localhost:${port}/index.html?track=/track`;

const CORRECTED_MS = 190_000; // song is 199.7 s; ~3:10 of it
const RAW_MS = 65_000;

async function runMode(browser, mode, durationMs) {
  const page = await browser.newPage();
  page.on('console', m => {
    const t = m.text();
    if (t.startsWith('PROBE_SUMMARY')) console.log(`[${mode}] ${t}`);
  });
  await page.goto(url);
  await page.waitForFunction(() => window.__probeReady === true, null, { timeout: 120_000 });
  await page.evaluate(m => window.__probe.setMode(m), mode);
  await page.evaluate(() => window.__probe.play());
  console.log(`[${mode}] playing for ${durationMs / 1000}s ...`);
  await page.waitForTimeout(durationMs);
  const report = await page.evaluate(() => {
    window.__probe.pause();
    return window.__probe.getReport();
  });
  const file = join(outDir, `skew-report-${mode}.json`);
  writeFileSync(file, JSON.stringify(report, null, 1));
  console.log(`[${mode}] wrote ${file}`);
  console.log(`[${mode}] SUMMARY ${JSON.stringify(report.summary)}`);
  await page.close();
  return report.summary;
}

const browser = await chromium.launch({
  channel: 'chrome',
  headless: false,
  args: ['--autoplay-policy=no-user-gesture-required', '--mute-audio'],
});

try {
  const corrected = await runMode(browser, 'corrected', CORRECTED_MS);
  const raw = await runMode(browser, 'raw', RAW_MS);
  writeFileSync(join(outDir, 'summaries.json'), JSON.stringify({ corrected, raw }, null, 2));
  console.log('DONE');
} finally {
  await browser.close();
}
