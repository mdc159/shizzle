/**
 * Resolve a manifest media reference to a fetchable URL.
 *
 * The server returns cloud-track manifests with absolute-path refs already
 * pointing at the same-origin CDN proxy (e.g. `/cdn/tracks/<id>/1/video.mp4`);
 * those are used verbatim. Local-profile manifests keep relative refs
 * (`video.mp4`, `stems/vocals.m4a`) and are joined with the track base path.
 */
export function resolveMediaUrl(baseUrl: string, file: string): string {
  if (/^https?:\/\//i.test(file) || file.startsWith('/')) {
    return file;
  }
  const base = baseUrl.replace(/\/+$/, '');
  return `${base}/${file}`;
}
