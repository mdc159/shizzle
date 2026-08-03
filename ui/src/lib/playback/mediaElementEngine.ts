/**
 * Media-element PlaybackEngine — desktop reference implementation.
 *
 * One HTMLAudioElement per stem, each routed:
 *   source -> stem GainNode -> master GainNode -> limiter -> destination
 *
 * - Stem gains are dB end-to-end (manifest `default_gain_db`, store faders),
 *   converted once via dbToLinear().
 * - Master bus carries a fixed -3 dB headroom under the user volume because
 *   decoded AAC stems overshoot ~+1 dBFS at unity (spike 0.4); the
 *   DynamicsCompressorNode is configured as a conservative transparent
 *   limiter that catches only summing overshoot.
 * - Drift/stall handling is the tiered policy from spike 0.1 (driftPolicy.ts),
 *   evaluated on a 1 s cadence against the attached video's clock.
 */

import type { Stem, StemId, StemsManifest } from '@/types/karaoke';
import type { PlaybackEngine } from './PlaybackEngine';
import type { PlaybackMetrics, StemMetrics } from './metrics';
import { dbToLinear } from './db';
import {
  HARD_DRIFT_SEC,
  STALL_TICKS_THRESHOLD,
  SYNC_INTERVAL_MS,
  VIDEO_ADVANCE_EPSILON_SEC,
  evaluateDrift,
} from './driftPolicy';

/** Fixed headroom under the user master gain (AAC overshoot, spike 0.4). */
const MASTER_HEADROOM_DB = -3;
/** Gain reduction beyond this counts as "limiter active" for the indicator. */
const LIMITER_ACTIVE_DB = 0.5;

const GAIN_SMOOTHING_SEC = 0.05;
const STEM_LOAD_TIMEOUT_MS = 15000;

interface StemChannel {
  id: StemId;
  el: HTMLAudioElement;
  source: MediaElementAudioSourceNode;
  gain: GainNode;
  gainDb: number;
  muted: boolean;
  soloed: boolean;
  waitingEvents: number;
  stalledEvents: number;
  hardSeeks: number;
}

class MediaElementEngine implements PlaybackEngine {
  private ctx: AudioContext | null = null;
  private masterGain: GainNode | null = null;
  private limiter: DynamicsCompressorNode | null = null;
  private channels = new Map<StemId, StemChannel>();
  private video: HTMLVideoElement | null = null;
  private masterGainDb = 0;
  private syncTimer: number | null = null;
  private lastVideoTime = 0;
  private stalledTicks = 0;
  private nudgeTicks = 0;
  private stallBailouts = 0;
  private peakReductionDb = 0;
  private stallCb: (() => void) | null = null;

  private async initialize(): Promise<void> {
    if (!this.ctx) {
      const Ctor =
        window.AudioContext ??
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext;
      this.ctx = new Ctor();

      // Master bus: stem gains -> masterGain (user volume + headroom) ->
      // limiter -> destination.
      this.masterGain = this.ctx.createGain();
      this.masterGain.gain.value = dbToLinear(this.masterGainDb + MASTER_HEADROOM_DB);

      this.limiter = this.ctx.createDynamicsCompressor();
      this.limiter.threshold.value = -1; // dBFS
      this.limiter.knee.value = 0; // hard knee: transparent below threshold
      this.limiter.ratio.value = 20; // effectively a limiter
      this.limiter.attack.value = 0.003; // 3 ms — fast enough for transients
      this.limiter.release.value = 0.05; // 50 ms

      this.masterGain.connect(this.limiter);
      this.limiter.connect(this.ctx.destination);
    }
    if (this.ctx.state === 'suspended') {
      await this.ctx.resume();
    }
  }

  async load(manifest: StemsManifest, baseUrl: string): Promise<void> {
    await this.initialize();
    this.releaseChannels();
    this.nudgeTicks = 0;
    this.stallBailouts = 0;
    this.peakReductionDb = 0;

    const loads = manifest.stems.map(async (stem: Stem) => {
      const el = new Audio();
      el.crossOrigin = 'anonymous';
      el.preload = 'auto';
      el.src = `${baseUrl}/${stem.file}`;

      const source = this.ctx!.createMediaElementSource(el);
      const gain = this.ctx!.createGain();
      // Contract fix (Phase 1.3): manifest gain is dB and converted here.
      // The legacy code assigned the raw value as a linear gain, so the
      // written `0` (meaning 0 dB) silenced every stem.
      const gainDb = stem.default_gain_db ?? 0;
      gain.gain.value = dbToLinear(gainDb);

      source.connect(gain);
      gain.connect(this.masterGain!);

      const channel: StemChannel = {
        id: stem.id,
        el,
        source,
        gain,
        gainDb,
        muted: false,
        soloed: false,
        waitingEvents: 0,
        stalledEvents: 0,
        hardSeeks: 0,
      };
      el.addEventListener('waiting', () => {
        channel.waitingEvents += 1;
      });
      el.addEventListener('stalled', () => {
        channel.stalledEvents += 1;
      });
      this.channels.set(stem.id, channel);

      // Wait for the element to be playable (15 s timeout, as salvaged).
      await new Promise<void>((resolve, reject) => {
        const timeout = window.setTimeout(() => {
          reject(new Error(`Stem load timeout: ${stem.file}`));
        }, STEM_LOAD_TIMEOUT_MS);
        el.addEventListener(
          'canplaythrough',
          () => {
            window.clearTimeout(timeout);
            resolve();
          },
          { once: true },
        );
        el.addEventListener(
          'error',
          () => {
            window.clearTimeout(timeout);
            reject(new Error(`Stem failed to load: ${stem.file}`));
          },
          { once: true },
        );
        el.load();
      });
    });

    await Promise.all(loads);
  }

  unload(): void {
    this.stopSyncLoop();
    this.releaseChannels();
  }

  attachVideo(video: HTMLVideoElement | null): void {
    this.video = video;
  }

  async play(): Promise<void> {
    await this.initialize();
    const video = this.video;
    if (video) {
      // Startup alignment: hard-correct only stems beyond the hard threshold
      // (identical to the salvaged pre-play syncToVideoTime call).
      this.hardSyncToVideo(video.currentTime, HARD_DRIFT_SEC);
      this.lastVideoTime = video.currentTime;
    }
    this.stalledTicks = 0;

    const results = await Promise.allSettled(
      Array.from(this.channels.values()).map((c) => c.el.play()),
    );
    const failures = results.filter((r) => r.status === 'rejected');
    if (failures.length > 0) {
      this.pause();
      throw new Error(`${failures.length} stem(s) failed to play`);
    }
    this.startSyncLoop();
  }

  pause(): void {
    this.stopSyncLoop();
    for (const c of this.channels.values()) {
      c.el.pause();
      c.el.playbackRate = 1;
    }
  }

  seek(t: number): void {
    for (const c of this.channels.values()) {
      c.el.currentTime = t;
    }
  }

  setStemGainDb(stem: StemId, db: number): void {
    const c = this.channels.get(stem);
    if (!c) return;
    c.gainDb = db;
    this.applyStemGains();
  }

  setStemMute(stem: StemId, muted: boolean): void {
    const c = this.channels.get(stem);
    if (!c) return;
    c.muted = muted;
    this.applyStemGains();
  }

  setStemSolo(stem: StemId, soloed: boolean): void {
    const c = this.channels.get(stem);
    if (!c) return;
    c.soloed = soloed;
    this.applyStemGains();
  }

  setMasterGainDb(db: number): void {
    this.masterGainDb = db;
    if (this.ctx && this.masterGain) {
      this.masterGain.gain.setTargetAtTime(
        dbToLinear(db + MASTER_HEADROOM_DB),
        this.ctx.currentTime,
        GAIN_SMOOTHING_SEC,
      );
    }
  }

  onStallBailout(cb: (() => void) | null): void {
    this.stallCb = cb;
  }

  getMetrics(): PlaybackMetrics {
    this.sampleLimiter();
    const stems: Partial<Record<StemId, StemMetrics>> = {};
    const vt = this.video ? this.video.currentTime : null;
    for (const c of this.channels.values()) {
      stems[c.id] = {
        skewMs: vt === null ? null : Math.round((c.el.currentTime - vt) * 1000),
        readyState: c.el.readyState,
        waitingEvents: c.waitingEvents,
        stalledEvents: c.stalledEvents,
        playbackRate: c.el.playbackRate,
        hardSeeks: c.hardSeeks,
        gainLinear: c.gain.gain.value,
      };
    }
    const reductionDb = this.limiter ? Math.max(0, -this.limiter.reduction) : 0;
    return {
      stems,
      nudgeTicks: this.nudgeTicks,
      stallBailouts: this.stallBailouts,
      masterGainDb: this.masterGainDb,
      masterHeadroomDb: MASTER_HEADROOM_DB,
      limiter: {
        active: reductionDb > LIMITER_ACTIVE_DB,
        reductionDb,
        peakReductionDb: this.peakReductionDb,
      },
    };
  }

  dispose(): void {
    this.unload();
    this.stallCb = null;
    this.video = null;
    this.masterGain?.disconnect();
    this.limiter?.disconnect();
    this.masterGain = null;
    this.limiter = null;
    if (this.ctx) {
      void this.ctx.close();
      this.ctx = null;
    }
  }

  // --- internals ---

  /** Mute/solo-aware gain application (ported from the salvaged useAudioSync). */
  private applyStemGains(): void {
    if (!this.ctx) return;
    const anySoloed = Array.from(this.channels.values()).some((c) => c.soloed);
    const now = this.ctx.currentTime;
    for (const c of this.channels.values()) {
      // Muted, or another stem is soloed and this one isn't -> silence.
      const silenced = c.muted || (anySoloed && !c.soloed);
      const target = silenced ? 0 : dbToLinear(c.gainDb);
      c.gain.gain.setTargetAtTime(target, now, GAIN_SMOOTHING_SEC);
    }
  }

  private hardSyncToVideo(videoTime: number, thresholdSec: number): void {
    for (const c of this.channels.values()) {
      const drift = Math.abs(c.el.currentTime - videoTime);
      if (drift > thresholdSec) {
        console.debug(`Syncing ${c.id}: drift was ${drift.toFixed(3)}s`);
        c.el.currentTime = videoTime;
        c.hardSeeks += 1;
      }
    }
  }

  private setRateAll(rate: number): void {
    for (const c of this.channels.values()) {
      c.el.playbackRate = rate;
    }
  }

  private averageCurrentTime(): number {
    const list = Array.from(this.channels.values());
    if (list.length === 0) return 0;
    return list.reduce((sum, c) => sum + c.el.currentTime, 0) / list.length;
  }

  private startSyncLoop(): void {
    this.stopSyncLoop();
    this.syncTimer = window.setInterval(() => {
      this.syncTick();
    }, SYNC_INTERVAL_MS);
  }

  private stopSyncLoop(): void {
    if (this.syncTimer !== null) {
      window.clearInterval(this.syncTimer);
      this.syncTimer = null;
    }
  }

  /** One evaluation of the tiered drift/stall policy (1 s cadence). */
  private syncTick(): void {
    this.sampleLimiter();
    const video = this.video;
    if (!video || this.channels.size === 0) return;

    const vt = video.currentTime;
    const advanced = vt > this.lastVideoTime + VIDEO_ADVANCE_EPSILON_SEC;
    this.lastVideoTime = vt;

    // If video is not advancing while playing, avoid repeated rewinds.
    if (!advanced) {
      this.stalledTicks += 1;
      if (this.stalledTicks >= STALL_TICKS_THRESHOLD) {
        this.stallBailouts += 1;
        this.pause();
        this.stalledTicks = 0;
        this.stallCb?.();
      }
      return;
    }
    this.stalledTicks = 0;

    const drift = this.averageCurrentTime() - vt;
    const action = evaluateDrift(drift);
    if (action.type === 'none') {
      this.setRateAll(1);
    } else if (action.type === 'nudge') {
      this.nudgeTicks += 1;
      this.setRateAll(action.rate);
    } else {
      // Severe drift: single hard correction, then continue in soft mode.
      this.hardSyncToVideo(vt, HARD_DRIFT_SEC);
      this.setRateAll(1);
    }
  }

  private sampleLimiter(): void {
    if (!this.limiter) return;
    const reduction = Math.max(0, -this.limiter.reduction);
    if (reduction > this.peakReductionDb) {
      this.peakReductionDb = reduction;
    }
  }

  private releaseChannels(): void {
    for (const c of this.channels.values()) {
      c.el.pause();
      c.el.src = '';
      c.source.disconnect();
      c.gain.disconnect();
    }
    this.channels.clear();
    this.stalledTicks = 0;
  }
}

/** Singleton engine instance (mirrors the salvaged audioManager singleton). */
export const playbackEngine: PlaybackEngine = new MediaElementEngine();
