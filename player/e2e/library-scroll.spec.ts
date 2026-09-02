import { test, expect } from '@playwright/test';

// Regression test for issue #3: the library drawer clipped everything below
// the fold and the mouse wheel did nothing, so tracks past the first screen
// were unreachable. Runs against the local dev server with a mocked control
// plane — no backend or credentials required.

const tracks = Array.from({ length: 27 }, (_, i) => ({
  id: `e2e-track-${i + 1}`,
  title: `Track ${i + 1}`,
  artist: `Artist ${i + 1}`,
  slug: `e2e-track-${i + 1}`,
  duration: 180 + i,
  publicUrl: `/tracks/e2e-track-${i + 1}`,
  status: 'ready' as const,
}));

test('library drawer scrolls to reveal all 27 tracks', async ({ page }) => {
  // The global config uses 15-minute timeouts sized for full-track playback;
  // this UI test needs seconds, not minutes.
  test.setTimeout(60_000);

  await page.addInitScript(() => {
    localStorage.setItem('shizzle_token', 'e2e-token');
  });
  await page.route('**/api/media/session', (route) =>
    route.fulfill({ json: { cloudfront: false } })
  );
  await page.route('**/api/library', (route) =>
    route.fulfill({ json: { tracks, total: tracks.length } })
  );

  await page.goto('/');
  await page.getByRole('button', { name: 'Library' }).click();

  const first = page.getByText('Track 1', { exact: true });
  const last = page.getByText('Track 27', { exact: true });

  await expect(first).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText('27 tracks available')).toBeVisible({ timeout: 5_000 });

  // The tail of the list starts out below the fold...
  await expect(last).not.toBeInViewport({ timeout: 5_000 });

  // ...and mouse-wheel scrolling over the drawer must reach it.
  await first.hover();
  for (let i = 0; i < 10; i++) {
    await page.mouse.wheel(0, 600);
  }
  await expect(last).toBeInViewport({ timeout: 5_000 });

  // The row is genuinely interactive at the bottom of the list.
  await expect(last).toBeVisible({ timeout: 5_000 });
});

test('library drawer supports search and sorting', async ({ page }) => {
  test.setTimeout(60_000);

  // API (recency) order: Duet Night (Beta Band), Apple Pie, Café Song,
  // Duet Night (Alpha Band). The fixture is chosen so that:
  // - title-asc hits a title tie (two "Duet Night" rows) broken by artist;
  // - artist-asc hits an artist tie (two "Beta Band" rows) broken by title
  //   and sends the empty-artist track ("Apple Pie") last;
  // - "Café Song" doubles as the diacritic search case.
  const unsortedTracks = [
    {
      id: 'track-duet-beta',
      title: 'Duet Night',
      artist: 'Beta Band',
      slug: 'track-duet-beta',
      duration: 200,
      publicUrl: '/tracks/track-duet-beta',
      status: 'ready' as const,
    },
    {
      id: 'track-apple',
      title: 'Apple Pie',
      artist: '',
      slug: 'track-apple',
      duration: 120,
      publicUrl: '/tracks/track-apple',
      status: 'ready' as const,
    },
    {
      id: 'track-cafe',
      title: 'Café Song',
      artist: 'Beta Band',
      slug: 'track-cafe',
      duration: 300,
      publicUrl: '/tracks/track-cafe',
      status: 'ready' as const,
    },
    {
      id: 'track-duet-alpha',
      title: 'Duet Night',
      artist: 'Alpha Band',
      slug: 'track-duet-alpha',
      duration: 180,
      publicUrl: '/tracks/track-duet-alpha',
      status: 'ready' as const,
    },
  ];

  await page.addInitScript(() => {
    localStorage.setItem('shizzle_token', 'e2e-token');
  });
  await page.route('**/api/media/session', (route) =>
    route.fulfill({ json: { cloudfront: false } })
  );
  await page.route('**/api/library', (route) =>
    route.fulfill({ json: { tracks: unsortedTracks, total: unsortedTracks.length } })
  );

  await page.goto('/');
  await page.getByRole('button', { name: 'Library' }).click();

  const rows = page.getByTestId('library-track-row');
  const sortSelect = page.getByRole('combobox', { name: 'Sort library' });
  const searchInput = page.getByRole('textbox', { name: 'Search library' });

  await expect(page.getByText('4 tracks available')).toBeVisible({ timeout: 5_000 });

  // Default order is the API order.
  await expect(rows).toHaveCount(4);
  await expect(rows.nth(0)).toContainText('Duet Night');
  await expect(rows.nth(0)).toContainText('Beta Band');
  await expect(rows.nth(1)).toContainText('Apple Pie');
  await expect(rows.nth(2)).toContainText('Café Song');
  await expect(rows.nth(3)).toContainText('Alpha Band');

  // Title sort: the "Duet Night" tie is broken by artist (Alpha before
  // Beta). The empty-artist track is unaffected — only artist order sends
  // it last.
  await sortSelect.selectOption('title-asc');
  await expect(rows.nth(0)).toContainText('Apple Pie');
  await expect(rows.nth(1)).toContainText('Café Song');
  await expect(rows.nth(2)).toContainText('Duet Night');
  await expect(rows.nth(2)).toContainText('Alpha Band');
  await expect(rows.nth(3)).toContainText('Duet Night');
  await expect(rows.nth(3)).toContainText('Beta Band');

  // Artist sort: the "Beta Band" tie is broken by title (Café before Duet)
  // and the empty-artist track lands last.
  await sortSelect.selectOption('artist-asc');
  await expect(rows.nth(0)).toContainText('Duet Night');
  await expect(rows.nth(0)).toContainText('Alpha Band');
  await expect(rows.nth(1)).toContainText('Café Song');
  await expect(rows.nth(2)).toContainText('Duet Night');
  await expect(rows.nth(2)).toContainText('Beta Band');
  await expect(rows.nth(3)).toContainText('Apple Pie');

  // The header count reflects the active filter.
  await searchInput.fill('duet');
  await expect(page.getByText('2 of 4 tracks')).toBeVisible({ timeout: 5_000 });
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toContainText('Duet Night');
  await expect(rows.nth(1)).toContainText('Duet Night');

  // Search folds diacritics: "cafe" matches "Café Song".
  await searchInput.fill('cafe');
  await expect(rows).toHaveCount(1);
  await expect(rows.nth(0)).toContainText('Café Song');

  // (Escape-inside-input behaviour is covered at the end of this test: the
  // first Escape clears the query and keeps the drawer open, a second Escape
  // closes it.)

  await searchInput.fill('zzz');
  await expect(page.getByText('No matching tracks')).toBeVisible({ timeout: 5_000 });
  await expect(rows).toHaveCount(0);

  // The clear button restores the full list and the total count.
  await page.getByRole('button', { name: 'Clear search' }).click();
  await expect(rows).toHaveCount(4);
  await expect(page.getByText('4 tracks available')).toBeVisible({ timeout: 5_000 });

  // The sort choice survives a page reload (localStorage persistence).
  await sortSelect.selectOption('artist-asc');
  await page.reload();
  await page.getByRole('button', { name: 'Library' }).click();
  await expect(sortSelect).toHaveValue('artist-asc', { timeout: 10_000 });
  await expect(rows.nth(0)).toContainText('Alpha Band', { timeout: 5_000 });

  // Escape with a non-empty query clears the query and the drawer stays
  // open: SheetContent passes onEscapeKeyDown to DialogPrimitive.Content,
  // where preventDefault cancels the dismiss. A second Escape with an empty
  // query closes the drawer as before.
  await searchInput.fill('duet');
  await expect(rows).toHaveCount(2);
  await searchInput.press('Escape');
  await expect(searchInput).toHaveValue('');
  await expect(searchInput).toBeVisible();
  await expect(rows).toHaveCount(4);
  await searchInput.press('Escape');
  await expect(searchInput).not.toBeVisible({ timeout: 5_000 });

  // Reopen for the keyboard assertion below.
  await page.getByRole('button', { name: 'Library' }).click();
  await expect(rows.first()).toBeVisible({ timeout: 5_000 });

  // Rows are keyboard-reachable: Enter on a focused row selects the track,
  // which closes the drawer (loadTrack sets activeDrawer to 'none'). Kept
  // last: loadTrack triggers an unmocked manifest fetch whose error toast
  // would overlay the Library button for later steps.
  await rows.first().focus();
  await page.keyboard.press('Enter');
  await expect(searchInput).not.toBeVisible({ timeout: 5_000 });
});
