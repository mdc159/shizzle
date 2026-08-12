/**
 * Playback instrumentation surface (spike 0.1 probe metrics, productized).
 *
 * Rule from the spike: metrics are reported PER STEM, never average-only —
 * an average hides the one stem that is drifting.
 */

import type { StemId } from '@/types/karaoke';

export interface StemMetrics {
  /** Stem clock minus video clock, in ms. Null when no video is attached. */
  skewMs: number | null;
  /** HTMLMediaElement.readyState (4 = HAVE_ENOUGH_DATA). */
  readyState: number;
  /** Count of 'waiting' (buffering) events since load. */
  waitingEvents: number;
  /** Count of 'stalled' events since load. */
  stalledEvents: number;
  playbackRate: number;
  /** Hard-seek corrections applied to this stem since load. */
  hardSeeks: number;
  /** Current linear gain of the stem's GainNode (0 = silenced by mute/solo). */
  gainLinear: number;
  /** Direct PCM RMS after this stem's gain, before the shared master bus. */
  signalRmsDbfs: number | null;
  paused: boolean;
  networkState: number;
  /** Seconds currently buffered beyond this stem's playhead. */
  bufferedAheadSec: number;
  /** MediaError code, or null when the element is healthy. */
  errorCode: number | null;
}

export interface LimiterMetrics {
  /** True when gain reduction exceeds 0.5 dB — drives the clip indicator. */
  active: boolean;
  /** Instantaneous gain reduction in dB (positive = limiting). */
  reductionDb: number;
  /** Peak gain reduction observed since load, in dB. */
  peakReductionDb: number;
}

export interface PlaybackMetrics {
  stems: Partial<Record<StemId, StemMetrics>>;
  /** Sync-loop ticks that applied a playbackRate nudge (whole-mix, by design). */
  nudgeTicks: number;
  /** Times the stall policy bailed out and paused playback. */
  stallBailouts: number;
  /** User master gain in dB (headroom excluded). */
  masterGainDb: number;
  /** Fixed headroom applied under the user gain (AAC overshoot, spike 0.4). */
  masterHeadroomDb: number;
  limiter: LimiterMetrics;
  desiredPlaying: boolean;
  health: PlaybackHealth;
  output: AudioOutputMetrics;
  video: VideoMetrics | null;
  /** Most recent first-party incidents; bounded to the latest 100 entries. */
  incidents: PlaybackIncident[];
}

export type PlaybackHealthStatus =
  | 'idle'
  | 'starting'
  | 'healthy'
  | 'recovering'
  | 'blocked'
  | 'failed';

export interface PlaybackHealth {
  status: PlaybackHealthStatus;
  recoveryAttempts: number;
  recoverySuccesses: number;
  lastHealthyAtMs: number | null;
}

export interface AudioOutputMetrics {
  /** Native AudioContext state, including WebKit's non-standard "interrupted". */
  contextState: string;
  rmsDbfs: number | null;
  peakDbfs: number | null;
  silentForMs: number;
}

export interface VideoMetrics {
  currentTime: number;
  paused: boolean;
  readyState: number;
  networkState: number;
  bufferedAheadSec: number;
  errorCode: number | null;
}

export type PlaybackIncidentCode =
  | 'play-rejected'
  | 'stem-media-error'
  | 'stem-clock-stalled'
  | 'video-media-error'
  | 'video-buffering'
  | 'video-clock-stalled'
  | 'audio-context-not-running'
  | 'render-silence'
  | 'recovery-started'
  | 'recovery-succeeded'
  | 'recovery-failed';

export interface PlaybackIncident {
  sequence: number;
  atMs: number;
  code: PlaybackIncidentCode;
  detail: string;
}
