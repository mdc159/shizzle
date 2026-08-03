/**
 * dB <-> linear gain conversions.
 *
 * The manifest contract carries gains in dB (`default_gain_db`); the Web Audio
 * graph wants linear multipliers. All conversion happens through these two
 * functions so a dB value can never be mistaken for a linear one again.
 */

export function dbToLinear(db: number): number {
  if (db === -Infinity) return 0;
  return Math.pow(10, db / 20);
}

export function linearToDb(value: number): number {
  if (value <= 0) return -Infinity;
  return 20 * Math.log10(value);
}
