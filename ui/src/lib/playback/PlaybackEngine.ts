/**
 * PlaybackEngine — the contract between the React UI and any stem-playback
 * implementation.
 *
 * The media-element engine (mediaElementEngine.ts) is the desktop reference
 * implementation validated by spike 0.1. If a constrained device (iPad,
 * projector) fails the skew bar, an aligned-segment-scheduler engine can be
 * swapped in behind this same interface.
 */

import type { StemId, StemsManifest } from '@/types/karaoke';
import type { PlaybackIncident, PlaybackMetrics } from './metrics';

export interface PlaybackEngine {
  /** Load all stems from a manifest. Replaces any previously loaded track. */
  load(manifest: StemsManifest, baseUrl: string): Promise<void>;
  /** Release the loaded track (elements, timers). The engine stays reusable. */
  unload(): void;
  /** Attach the video element whose clock is the sync master (null detaches). */
  attachVideo(video: HTMLVideoElement | null): void;
  play(): Promise<void>;
  pause(): void;
  seek(t: number): void;
  /** Per-stem fader value in dB (-60 displays as -infinity). */
  setStemGainDb(stem: StemId, db: number): void;
  setStemMute(stem: StemId, muted: boolean): void;
  setStemSolo(stem: StemId, soloed: boolean): void;
  /** User master gain in dB; the engine adds its own fixed headroom under it. */
  setMasterGainDb(db: number): void;
  /** Callback fired when the stall policy pauses playback (null clears). */
  onStallBailout(cb: (() => void) | null): void;
  /** First-party playback-health incident stream (null clears). */
  onIncident(cb: ((incident: PlaybackIncident) => void) | null): void;
  getMetrics(): PlaybackMetrics;
  /** Full teardown including the AudioContext. */
  dispose(): void;
}
