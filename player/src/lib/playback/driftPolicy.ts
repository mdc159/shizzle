/**
 * Shared playback timing constants — pure numbers, no DOM.
 *
 * HARD_DRIFT_SEC is the synchronization threshold of the audio-master
 * alignment: the silent video is moved to the stems' audible position when it
 * is this far or further from it, and a stem's audible offset from the video
 * counts as in sync below it. MAX_INTER_STEM_SKEW_SEC is the audible
 * stem-to-stem spread at which a device's starts count as needing alignment;
 * below it the stems are left exactly as they start. The remaining constants
 * pace the video-clock stall loop (syncTick) that runs alongside the
 * watchdog.
 */

export const HARD_DRIFT_SEC = 0.04;
export const MAX_INTER_STEM_SKEW_SEC = 0.04;
export const STALL_TICKS_THRESHOLD = 3;
export const VIDEO_ADVANCE_EPSILON_SEC = 0.02;
export const SYNC_INTERVAL_MS = 1000;
