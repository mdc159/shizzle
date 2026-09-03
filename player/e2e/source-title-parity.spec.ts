import { test, expect } from '@playwright/test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

// Parser parity: the browser-side parseSourceTitle must agree with the
// control plane's parser on every golden row. The fixture is a verbatim copy
// of library/tests/fixtures/source_titles.json (backend is source of truth);
// parser_* fields are that parser's pinned outputs.

interface GoldenRow {
  id: string;
  raw: string;
  parser_artist: string;
  parser_title: string;
  parser_confident: boolean;
}

const fixture = JSON.parse(
  readFileSync(fileURLToPath(new URL('fixtures/source_titles.json', import.meta.url)), 'utf-8')
) as { rows: GoldenRow[] };

// Served and transformed on the fly by the Vite dev server. Kept in a
// variable so the browser-native dynamic import stays verbatim (knip cannot
// resolve Vite's absolute /src paths and would flag a literal specifier).
const SOURCE_TITLE_MODULE = '/src/lib/sourceTitle.ts';

interface ParseResult {
  artist: string;
  title: string;
  confident: boolean;
}

for (const row of fixture.rows) {
  test(`parity: ${row.raw}`, async ({ page }) => {
    test.setTimeout(60_000);

    // The page is just a module host: the Vite dev server transforms and
    // serves the module, so the test exercises the exact code the upload
    // dialog ships.
    await page.goto('/');
    const result = await page.evaluate(
      async ({ raw, modulePath }: { raw: string; modulePath: string }) => {
        const { parseSourceTitle } = (await import(modulePath)) as {
          parseSourceTitle: (value: string) => ParseResult;
        };
        return parseSourceTitle(raw);
      },
      { raw: row.raw, modulePath: SOURCE_TITLE_MODULE }
    );

    expect(result).toEqual({
      artist: row.parser_artist,
      title: row.parser_title,
      confident: row.parser_confident,
    });
  });
}
