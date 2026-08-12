/**
 * Tiered drift policy — pure math, no DOM.
 *
 * Ported faithfully from the salvaged useAudioSync.ts and validated by spike
 * 0.1 (desktop Chrome: 3 ms max inter-stem skew corrected, one startup hard
 * seek, zero steady-state corrections). Do not "improve" the constants or the
 * tiers without re-running the skew probe.
 *
 * Tiers (drift = one stem clock - video clock):
 *   |drift| < 30 ms -> ignore, playbackRate 1
 *   |drift| < 40 ms -> playbackRate nudge, at most +/-0.5 %
 *   |drift| >= 40 ms -> single hard seek, then back to soft mode
 *
 * A 50-100 ms nudge tier was too slow for the production contract: a measured
 * -77 ms Pot stem remained outside the <=50 ms settled gate after 3 seconds.
 */

export const SOFT_DRIFT_SEC = 0.03;
export const HARD_DRIFT_SEC = 0.04;
export const NUDGE_MAX = 0.005;
export const NUDGE_FACTOR = 0.03;
export const STALL_TICKS_THRESHOLD = 3;
export const VIDEO_ADVANCE_EPSILON_SEC = 0.02;
export const SYNC_INTERVAL_MS = 1000;

export type DriftAction =
  | { type: 'none' }
  | { type: 'nudge'; rate: number }
  | { type: 'hardSeek' };

export function evaluateDrift(driftSec: number): DriftAction {
  const abs = Math.abs(driftSec);
  if (abs < SOFT_DRIFT_SEC) {
    return { type: 'none' };
  }
  if (abs < HARD_DRIFT_SEC) {
    // Gentle correction: slightly speed up / slow down stems to converge.
    const nudge = Math.max(-NUDGE_MAX, Math.min(NUDGE_MAX, -driftSec * NUDGE_FACTOR));
    return { type: 'nudge', rate: 1 + nudge };
  }
  return { type: 'hardSeek' };
}
