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
import type {
  PlaybackHealthStatus,
  PlaybackIncident,
  PlaybackIncidentCode,
  PlaybackMetrics,
  StemMetrics,
} from './metrics';
import { dbToLinear } from './db';
import { resolveMediaUrl } from './mediaUrl';
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
/**
 * Streaming load gate (design spec §5: streaming, not full preload).
 *
 * A stem is "loaded" as soon as it CAN begin playing (`canplay`,
 * readyState >= HAVE_FUTURE_DATA) — never when the browser has buffered the
 * whole file (`canplaythrough`), which on a large or slow object may simply
 * never happen and wedged the entire player. Buffering continues via CDN
 * Range requests while playback runs; the drift/stall policy handles hiccups.
 */
/** Proceed-while-buffering safety valve: after this long, any decoded data is enough to start. */
const STEM_START_SAFETY_MS = 8000;
/** Hard failure: nothing usable arrived at all within this window. */
const STEM_LOAD_TIMEOUT_MS = 15000;
const WATCHDOG_INTERVAL_MS = 100;
const CLOCK_PROGRESS_EPSILON_SEC = 0.03;
const CLOCK_STALL_MS = 1000;
const RECOVERY_STEM_READY_TIMEOUT_MS = 1500;
const MASTER_SEEK_HEAD_START_MS = 200;
/** Audible stem-to-stem separation is never hidden by a good average. */
const MAX_INTER_STEM_SKEW_SEC = 0.04;
const RENDER_SILENCE_DBFS = -90;
// A short digital-silence passage is valid music. Five seconds is long enough
// to catch a dead Web Audio graph without "repairing" ordinary rests.
const RENDER_SILENCE_MS = 5000;
const RECOVERY_COOLDOWN_MS = 1000;
const INCIDENT_LIMIT = 100;

interface StemChannel {
  id: StemId;
  el: HTMLAudioElement;
  source: MediaElementAudioSourceNode;
  gain: GainNode;
  analyser: AnalyserNode;
  analyserData: Float32Array<ArrayBuffer>;
  rmsDbfs: number | null;
  gainDb: number;
  muted: boolean;
  soloed: boolean;
  waitingEvents: number;
  stalledEvents: number;
  hardSeeks: number;
  /** True before intentional teardown clears src and may emit MediaError 4. */
  released: boolean;
}

class MediaElementEngine implements PlaybackEngine {
  private ctx: AudioContext | null = null;
  private masterGain: GainNode | null = null;
  private limiter: DynamicsCompressorNode | null = null;
  private analyser: AnalyserNode | null = null;
  private analyserData: Float32Array<ArrayBuffer> | null = null;
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
  private incidentCb: ((incident: PlaybackIncident) => void) | null = null;
  private desiredPlaying = false;
  private commandVersion = 0;
  private watchdogTimer: number | null = null;
  private lastWatchdogAt = 0;
  private lastWatchdogVideoTime = 0;
  private lastStemTimes = new Map<StemId, number>();
  private stemNoProgressMs = new Map<StemId, number>();
  private videoNoProgressMs = 0;
  private silentForMs = 0;
  private rmsDbfs: number | null = null;
  private peakDbfs: number | null = null;
  private inputSignalPresent = false;
  private healthStatus: PlaybackHealthStatus = 'idle';
  private recoveryAttempts = 0;
  private recoverySuccesses = 0;
  private lastHealthyAtMs: number | null = null;
  private recoveryInFlight = false;
  private lastRecoveryAt = 0;
  private recoveryRetryTimer: number | null = null;
  private videoBufferingForRecovery = false;
  private pendingSeekTarget: number | null = null;
  private stemPrefetchTimer: number | null = null;
  private incidentSequence = 0;
  private incidents: PlaybackIncident[] = [];

  private initialize(): void {
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

      // Direct output sensor. This samples the actual post-limiter PCM bus,
      // catching the important failure mode where media clocks advance but
      // Web Audio renders silence.
      this.analyser = this.ctx.createAnalyser();
      this.analyser.fftSize = 2048;
      this.analyser.smoothingTimeConstant = 0.1;
      this.analyserData = new Float32Array(this.analyser.fftSize);

      this.masterGain.connect(this.limiter);
      this.limiter.connect(this.analyser);
      this.analyser.connect(this.ctx.destination);
    }
  }

  async load(manifest: StemsManifest, baseUrl: string): Promise<void> {
    // Build the graph while loading, but do not resume it here. WebKit requires
    // resume() to be called from the eventual Play gesture.
    this.initialize();
    this.resetTrackSession();
    this.releaseChannels();
    this.nudgeTicks = 0;
    this.stallBailouts = 0;
    this.peakReductionDb = 0;
    this.resetWatchdogBaselines();

    const loads = manifest.stems.map(async (stem: Stem) => {
      const el = new Audio();
      el.crossOrigin = 'anonymous';
      el.preload = 'auto';
      el.src = resolveMediaUrl(baseUrl, stem.file);

      const source = this.ctx!.createMediaElementSource(el);
      const gain = this.ctx!.createGain();
      const analyser = this.ctx!.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.1;
      const analyserData = new Float32Array(analyser.fftSize);
      // Contract fix (Phase 1.3): manifest gain is dB and converted here.
      // The legacy code assigned the raw value as a linear gain, so the
      // written `0` (meaning 0 dB) silenced every stem.
      const gainDb = stem.default_gain_db ?? 0;
      gain.gain.value = dbToLinear(gainDb);

      source.connect(gain);
      gain.connect(analyser);
      analyser.connect(this.masterGain!);

      const channel: StemChannel = {
        id: stem.id,
        el,
        source,
        gain,
        analyser,
        analyserData,
        rmsDbfs: null,
        gainDb,
        muted: false,
        soloed: false,
        waitingEvents: 0,
        stalledEvents: 0,
        hardSeeks: 0,
        released: false,
      };
      el.addEventListener('waiting', () => {
        if (channel.released) return;
        channel.waitingEvents += 1;
      });
      el.addEventListener('stalled', () => {
        if (channel.released) return;
        channel.stalledEvents += 1;
      });
      el.addEventListener('error', () => {
        // Clearing an HTMLMediaElement src during an intentional track swap
        // emits MediaError 4 ("Empty src attribute") in Chromium. That is a
        // teardown signal, not a delivery/decode failure.
        if (channel.released) return;
        this.recordIncident(
          'stem-media-error',
          `${stem.id}: MediaError ${el.error?.code ?? 'unknown'} ${el.error?.message ?? ''}`.trim(),
        );
      });
      this.channels.set(stem.id, channel);

      // Wait until the element CAN begin playing — NOT until it has buffered
      // the entire file. `canplaythrough` was the old gate and it wedged the
      // whole player on any large/slow stem (573 MB of WAV never finishes
      // buffering); `canplay` fires at HAVE_FUTURE_DATA and the element keeps
      // streaming via Range requests while playback runs.
      await new Promise<void>((resolve, reject) => {
        let settled = false;
        const timers: number[] = [];
        const finish = (err?: Error) => {
          if (settled) return;
          settled = true;
          for (const t of timers) window.clearTimeout(t);
          el.removeEventListener('canplay', onCanPlay);
          el.removeEventListener('error', onError);
          if (err) reject(err);
          else resolve();
        };
        const onCanPlay = () => finish();
        const onError = () => finish(new Error(`Stem failed to load: ${stem.file}`));

        // Safety valve: a stem that is trickling in (has decoded *something*
        // but hasn't reached canplay yet) must not wedge the whole load —
        // start anyway and let it keep buffering.
        timers.push(
          window.setTimeout(() => {
            if (el.readyState >= HTMLMediaElement.HAVE_CURRENT_DATA) finish();
          }, STEM_START_SAFETY_MS),
        );
        // Hard timeout: nothing usable arrived at all — a real load failure.
        timers.push(
          window.setTimeout(() => {
            finish(new Error(`Stem load timeout: ${stem.file}`));
          }, STEM_LOAD_TIMEOUT_MS),
        );

        el.addEventListener('canplay', onCanPlay);
        el.addEventListener('error', onError);
        el.load();
        if (el.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) finish();
      });
    });

    await Promise.all(loads);
  }

  unload(): void {
    this.resetTrackSession();
    this.releaseChannels();
  }

  attachVideo(video: HTMLVideoElement | null): void {
    if (this.video) this.detachVideoSensors(this.video);
    this.video = video;
    if (video) this.attachVideoSensors(video);
  }

  async play(): Promise<void> {
    if (!this.ctx || this.channels.size === 0) {
      throw new Error('Playback engine is not loaded');
    }
    this.desiredPlaying = true;
    this.healthStatus = 'starting';
    const version = ++this.commandVersion;
    const video = this.video;
    if (video) {
      // Startup alignment: hard-correct only stems beyond the hard threshold
      // (identical to the salvaged pre-play syncToVideoTime call).
      this.hardSyncToVideo(video.currentTime, HARD_DRIFT_SEC);
      this.lastVideoTime = video.currentTime;
    }
    this.stalledTicks = 0;
    // Arm direct sensors before asynchronous play promises settle. A WebKit
    // `waiting` event can invalidate this start and hand control to recovery;
    // observability must remain live across that handoff.
    this.startWatchdog();

    // Resume the AudioContext and start every media element synchronously in
    // this call stack. On iPad/WebKit this must remain inside the originating
    // user gesture; awaiting context.resume() before calling el.play() loses
    // that permission.
    const attempts: Array<{ label: string; promise: Promise<unknown> }> = [];
    if (this.ctx.state !== 'running') {
      attempts.push({ label: 'audio-context', promise: this.ctx.resume() });
    }
    for (const c of this.channels.values()) {
      attempts.push({ label: c.id, promise: c.el.play() });
    }
    const results = await Promise.allSettled(attempts.map((attempt) => attempt.promise));
    if (version !== this.commandVersion || !this.desiredPlaying) return;

    const failures = results.flatMap((result, index) =>
      result.status === 'rejected'
        ? [`${attempts[index].label}: ${this.describeError(result.reason)}`]
        : [],
    );
    if (failures.length > 0) {
      this.healthStatus = failures.some((failure) => failure.includes('NotAllowedError'))
        ? 'blocked'
        : 'failed';
      const detail = failures.join('; ');
      this.recordIncident('play-rejected', detail);
      throw new Error(`Playback start rejected — ${detail}`);
    }
    this.startSyncLoop();
    this.healthStatus = 'starting';
  }

  pause(): void {
    this.desiredPlaying = false;
    this.commandVersion += 1;
    this.healthStatus = 'idle';
    this.stopSyncLoop();
    this.stopWatchdog();
    for (const c of this.channels.values()) {
      c.el.pause();
      c.el.playbackRate = 1;
    }
  }

  seek(t: number): void {
    // A user seek starts a new recovery window. Reusing the cooldown from the
    // previous seek can add almost a full second and violate the 3 s settled
    // acceptance gate during rapid scrubbing.
    this.resetRecoveryCooldown();
    if (this.desiredPlaying) {
      // The video is the authoritative clock and its target frame gates every
      // audible decoder. Do not launch seven competing Range seeks at once:
      // let the video fetch/advance first, then recover() hard-seeks all six
      // stems together at the observed master time.
      this.healthStatus = 'recovering';
      this.videoBufferingForRecovery = true;
      this.pendingSeekTarget = t;
      for (const c of this.channels.values()) c.el.pause();
      if (this.stemPrefetchTimer !== null) window.clearTimeout(this.stemPrefetchTimer);
      this.stemPrefetchTimer = window.setTimeout(() => {
        this.stemPrefetchTimer = null;
        if (!this.desiredPlaying || this.pendingSeekTarget !== t) return;
        for (const c of this.channels.values()) {
          if (Math.abs(c.el.currentTime - t) < HARD_DRIFT_SEC) continue;
          c.el.currentTime = t;
          c.hardSeeks += 1;
        }
      }, MASTER_SEEK_HEAD_START_MS);
    } else {
      this.pendingSeekTarget = null;
      for (const c of this.channels.values()) c.el.currentTime = t;
    }
    this.resetWatchdogBaselines();
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

  onIncident(cb: ((incident: PlaybackIncident) => void) | null): void {
    this.incidentCb = cb;
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
        signalRmsDbfs: c.rmsDbfs,
        paused: c.el.paused,
        networkState: c.el.networkState,
        bufferedAheadSec: this.bufferedAhead(c.el),
        errorCode: c.el.error?.code ?? null,
      };
    }
    // Chromium may expose a stale/default compressor reduction while the graph
    // is idle. It is not a measurement until requested playback has produced
    // directly observed post-limiter PCM.
    const reductionDb = this.limiter && this.desiredPlaying && this.rmsDbfs !== null
      ? Math.max(0, -this.limiter.reduction)
      : 0;
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
      desiredPlaying: this.desiredPlaying,
      health: {
        status: this.healthStatus,
        recoveryAttempts: this.recoveryAttempts,
        recoverySuccesses: this.recoverySuccesses,
        lastHealthyAtMs: this.lastHealthyAtMs,
      },
      output: {
        contextState: this.ctx?.state ?? 'closed',
        rmsDbfs: this.rmsDbfs,
        peakDbfs: this.peakDbfs,
        silentForMs: this.silentForMs,
      },
      video: this.video
        ? {
            currentTime: this.video.currentTime,
            paused: this.video.paused,
            readyState: this.video.readyState,
            networkState: this.video.networkState,
            bufferedAheadSec: this.bufferedAhead(this.video),
            errorCode: this.video.error?.code ?? null,
          }
        : null,
      incidents: [...this.incidents],
    };
  }

  dispose(): void {
    this.unload();
    this.stallCb = null;
    this.incidentCb = null;
    if (this.video) this.detachVideoSensors(this.video);
    this.video = null;
    this.masterGain?.disconnect();
    this.limiter?.disconnect();
    this.analyser?.disconnect();
    this.masterGain = null;
    this.limiter = null;
    this.analyser = null;
    this.analyserData = null;
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

    // Buffering is not a clock failure. The direct watchdog records the wait
    // and resumes stems when the video has future data again.
    if (!advanced && video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) {
      this.stalledTicks = 0;
      return;
    }

    // If video is not advancing while playing, avoid repeated rewinds.
    if (!advanced) {
      this.stalledTicks += 1;
      if (this.stalledTicks >= STALL_TICKS_THRESHOLD) {
        this.stallBailouts += 1;
        this.stalledTicks = 0;
        this.recordIncident('video-clock-stalled', 'Video master clock did not advance');
        void this.recover('video-clock-stalled');
      }
      return;
    }
    this.stalledTicks = 0;

    const channels = Array.from(this.channels.values());
    const times = channels.map((c) => c.el.currentTime);
    if (Math.max(...times) - Math.min(...times) >= MAX_INTER_STEM_SKEW_SEC) {
      // The former average-clock policy could hide equal-and-opposite drift.
      // Correct the whole audible ensemble together at the video master.
      this.hardSyncToVideo(vt, 0);
      this.setRateAll(1);
      return;
    }

    let nudged = false;
    for (const c of channels) {
      const action = evaluateDrift(c.el.currentTime - vt);
      if (action.type === 'none') {
        c.el.playbackRate = 1;
      } else if (action.type === 'nudge') {
        c.el.playbackRate = action.rate;
        nudged = true;
      } else {
        c.el.currentTime = vt;
        c.el.playbackRate = 1;
        c.hardSeeks += 1;
      }
    }
    if (nudged) this.nudgeTicks += 1;
  }

  private sampleLimiter(): void {
    if (!this.limiter || !this.desiredPlaying || this.rmsDbfs === null) return;
    const reduction = Math.max(0, -this.limiter.reduction);
    if (reduction > this.peakReductionDb) {
      this.peakReductionDb = reduction;
    }
  }

  private startWatchdog(): void {
    this.stopWatchdog();
    this.resetWatchdogBaselines();
    this.watchdogTimer = window.setInterval(() => this.watchdogTick(), WATCHDOG_INTERVAL_MS);
  }

  private stopWatchdog(): void {
    if (this.watchdogTimer !== null) {
      window.clearInterval(this.watchdogTimer);
      this.watchdogTimer = null;
    }
    if (this.recoveryRetryTimer !== null) {
      window.clearTimeout(this.recoveryRetryTimer);
      this.recoveryRetryTimer = null;
    }
  }

  private watchdogTick(): void {
    if (!this.desiredPlaying) return;
    const now = performance.now();
    const elapsed = Math.max(0, now - this.lastWatchdogAt);
    this.lastWatchdogAt = now;
    this.sampleOutput(elapsed);

    if (this.ctx?.state !== 'running') {
      this.recordIncident('audio-context-not-running', `AudioContext state=${this.ctx?.state}`);
      void this.recover('audio-context-not-running');
      return;
    }

    const video = this.video;
    const videoAdvanced = Boolean(
      video && video.currentTime > this.lastWatchdogVideoTime + CLOCK_PROGRESS_EPSILON_SEC,
    );
    if (video) {
      this.videoNoProgressMs = videoAdvanced ? 0 : this.videoNoProgressMs + elapsed;
      this.lastWatchdogVideoTime = video.currentTime;
      if (video.error) {
        this.recordIncident('video-media-error', `MediaError ${video.error.code} ${video.error.message}`);
        this.healthStatus = 'failed';
        return;
      }
      if (this.videoBufferingForRecovery) {
        // Downloaded ranges and readyState can overstate usability at a random
        // seek. The authoritative recovery signal is the master clock itself.
        if (videoAdvanced && !video.paused && this.videoReachedPendingSeek(video)) {
          this.videoBufferingForRecovery = false;
          this.resetRecoveryCooldown();
          void this.recover('video-buffering');
        }
        return;
      }
      if (video.paused && !video.ended) {
        this.recordIncident('video-clock-stalled', 'Video paused while playback was requested');
        void this.recover('video-clock-stalled');
        return;
      }
      if (video.readyState < HTMLMediaElement.HAVE_FUTURE_DATA) {
        // A seek or real network wait is in progress. `playing` will restart
        // paused stems; do not claim health or manufacture clock failures.
        this.healthStatus = 'recovering';
        return;
      }
      if (this.videoNoProgressMs >= CLOCK_STALL_MS) {
        this.recordIncident('video-clock-stalled', `No progress for ${Math.round(this.videoNoProgressMs)} ms`);
        void this.recover('video-clock-stalled');
        return;
      }
    }

    let allStemsAdvanced = true;
    for (const c of this.channels.values()) {
      const prior = this.lastStemTimes.get(c.id) ?? c.el.currentTime;
      const advanced = c.el.currentTime > prior + CLOCK_PROGRESS_EPSILON_SEC;
      if (!advanced) allStemsAdvanced = false;
      const stalledFor = advanced ? 0 : (this.stemNoProgressMs.get(c.id) ?? 0) + elapsed;
      this.lastStemTimes.set(c.id, c.el.currentTime);
      this.stemNoProgressMs.set(c.id, stalledFor);
      if (c.el.error) {
        this.healthStatus = 'failed';
        return;
      }
      if ((c.el.paused || stalledFor >= CLOCK_STALL_MS) && videoAdvanced) {
        if (this.healthStatus !== 'recovering') {
          this.recordIncident(
            'stem-clock-stalled',
            `${c.id}: paused=${c.el.paused} noProgressMs=${Math.round(stalledFor)} readyState=${c.el.readyState}`,
          );
        }
        void this.recover('stem-clock-stalled');
        return;
      }
    }

    if (this.silentForMs >= RENDER_SILENCE_MS && videoAdvanced && this.inputSignalPresent) {
      this.recordIncident('render-silence', `Post-limiter PCM below ${RENDER_SILENCE_DBFS} dBFS`);
      void this.recover('render-silence');
      return;
    }
    if (video && videoAdvanced) {
      const stemTimes = Array.from(this.channels.values()).map((c) => c.el.currentTime);
      const interStemSkew = Math.max(...stemTimes) - Math.min(...stemTimes);
      const maxVideoOffset = Math.max(...stemTimes.map((time) => Math.abs(time - video.currentTime)));
      if (
        interStemSkew >= MAX_INTER_STEM_SKEW_SEC ||
        maxVideoOffset >= HARD_DRIFT_SEC
      ) {
        // Health includes synchronization. Recovery can resolve each play()
        // while decoders begin advancing on different samples; correct that
        // immediately instead of waiting for the one-second drift loop.
        this.hardSyncToVideo(video.currentTime, 0);
        this.setRateAll(1);
        this.healthStatus = 'starting';
        this.resetWatchdogBaselines();
        return;
      }
    }
    // Healthy is an observation, not an optimistic default: the master and
    // every decoder must all be advancing in this sample.
    const outputExpected = this.inputSignalPresent;
    const outputPresent = this.rmsDbfs !== null && this.rmsDbfs >= RENDER_SILENCE_DBFS;
    if (videoAdvanced && allStemsAdvanced && (!outputExpected || outputPresent)) this.markHealthy();
  }

  private sampleOutput(elapsedMs: number): void {
    if (!this.analyser || !this.analyserData) return;
    let inputSignalPresent = false;
    for (const c of this.channels.values()) {
      c.analyser.getFloatTimeDomainData(c.analyserData);
      let stemSumSquares = 0;
      for (const sample of c.analyserData) stemSumSquares += sample * sample;
      const stemRms = Math.sqrt(stemSumSquares / c.analyserData.length);
      c.rmsDbfs = stemRms > 0 ? 20 * Math.log10(stemRms) : null;
      if (c.rmsDbfs !== null && c.rmsDbfs >= RENDER_SILENCE_DBFS) inputSignalPresent = true;
    }
    this.inputSignalPresent = inputSignalPresent;
    this.analyser.getFloatTimeDomainData(this.analyserData);
    let sumSquares = 0;
    let peak = 0;
    for (const sample of this.analyserData) {
      sumSquares += sample * sample;
      peak = Math.max(peak, Math.abs(sample));
    }
    const rms = Math.sqrt(sumSquares / this.analyserData.length);
    this.rmsDbfs = rms > 0 ? 20 * Math.log10(rms) : null;
    this.peakDbfs = peak > 0 ? 20 * Math.log10(peak) : null;
    const outputPresent = this.rmsDbfs !== null && this.rmsDbfs >= RENDER_SILENCE_DBFS;
    this.silentForMs = this.inputSignalPresent && !outputPresent ? this.silentForMs + elapsedMs : 0;
  }

  private async recover(reason: PlaybackIncidentCode): Promise<void> {
    if (!this.desiredPlaying || this.recoveryInFlight) return;
    const now = performance.now();
    const retryIn = RECOVERY_COOLDOWN_MS - (now - this.lastRecoveryAt);
    if (retryIn > 0) {
      if (this.recoveryRetryTimer === null) {
        this.recoveryRetryTimer = window.setTimeout(() => {
          this.recoveryRetryTimer = null;
          void this.recover(reason);
        }, retryIn + 10);
      }
      return;
    }
    this.lastRecoveryAt = now;
    this.recoveryInFlight = true;
    this.recoveryAttempts += 1;
    this.healthStatus = 'recovering';
    this.recordIncident('recovery-started', reason);
    this.stopWatchdog();
    this.stopSyncLoop();
    const version = ++this.commandVersion;
    try {
      const coordinatedSeek = reason === 'video-buffering' && this.video && this.lastHealthyAtMs !== null;
      const target = coordinatedSeek
        ? this.video!.currentTime
        : this.pendingSeekTarget ?? this.video?.currentTime ?? this.averageCurrentTime();
      if (coordinatedSeek) {
        if (this.stemPrefetchTimer !== null) {
          window.clearTimeout(this.stemPrefetchTimer);
          this.stemPrefetchTimer = null;
        }
        this.video!.pause();
        for (const c of this.channels.values()) {
          c.el.pause();
          if (Math.abs(c.el.currentTime - target) >= HARD_DRIFT_SEC) {
            c.el.currentTime = target;
            c.hardSeeks += 1;
          }
        }
        await Promise.all(
          Array.from(this.channels.values()).map((c) => this.waitForSeekReady(c.el)),
        );
      } else {
        this.hardSyncToVideo(target, 0);
      }
      if (version !== this.commandVersion || !this.desiredPlaying) return;
      if (this.ctx && this.ctx.state !== 'running') await this.ctx.resume();
      for (let round = 0; round < 3; round += 1) {
        const attempts: Promise<unknown>[] = [];
        if (this.video && !this.video.ended && this.video.paused) attempts.push(this.video.play());
        for (const c of this.channels.values()) {
          if (c.el.paused) attempts.push(c.el.play());
        }
        const results = await Promise.allSettled(attempts);
        if (version !== this.commandVersion || !this.desiredPlaying) return;
        const failures = results.filter((result) => result.status === 'rejected');
        if (failures.length > 0) {
          throw new Error(failures.map((failure) => this.describeError(failure.reason)).join('; '));
        }
        const pausedStems = Array.from(this.channels.values()).filter((c) => c.el.paused);
        if (!this.video?.paused && pausedStems.length === 0) break;
        if (this.video && !this.video.paused && pausedStems.length > 0) {
          this.hardSyncToVideo(this.video.currentTime, 0);
          await Promise.all(pausedStems.map((c) => this.waitForSeekReady(c.el)));
        }
      }
      if (this.video) this.hardSyncToVideo(this.video.currentTime, HARD_DRIFT_SEC);
      if (this.video?.paused || Array.from(this.channels.values()).some((c) => c.el.paused)) {
        throw new Error('A media element remained paused after recovery');
      }
      this.recoverySuccesses += 1;
      this.pendingSeekTarget = null;
      this.videoBufferingForRecovery = false;
      this.startWatchdog();
      this.startSyncLoop();
      this.healthStatus = 'starting';
      this.recordIncident('recovery-succeeded', reason);
    } catch (error) {
      this.healthStatus = this.describeError(error).includes('NotAllowedError') ? 'blocked' : 'failed';
      this.recordIncident('recovery-failed', `${reason}: ${this.describeError(error)}`);
      this.stallCb?.();
    } finally {
      this.recoveryInFlight = false;
    }
  }

  private waitForSeekReady(el: HTMLMediaElement): Promise<void> {
    if (!el.seeking && el.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) {
      return Promise.resolve();
    }
    return new Promise<void>((resolve, reject) => {
      let settled = false;
      const finish = (error?: Error) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timer);
        el.removeEventListener('canplay', onReady);
        el.removeEventListener('seeked', onReady);
        el.removeEventListener('error', onError);
        if (error) reject(error);
        else resolve();
      };
      const onReady = () => {
        if (!el.seeking && el.readyState >= HTMLMediaElement.HAVE_FUTURE_DATA) finish();
      };
      const onError = () => finish(new Error(`MediaError ${el.error?.code ?? 'unknown'}`));
      const timer = window.setTimeout(
        () => finish(new Error('Stem seek did not become decodable within recovery window')),
        RECOVERY_STEM_READY_TIMEOUT_MS,
      );
      el.addEventListener('canplay', onReady);
      el.addEventListener('seeked', onReady);
      el.addEventListener('error', onError);
      onReady();
    });
  }

  private recordIncident(code: PlaybackIncidentCode, detail: string): void {
    const previous = this.incidents[this.incidents.length - 1];
    if (previous?.code === code && previous.detail === detail && Date.now() - previous.atMs < 1000) {
      return;
    }
    const incident: PlaybackIncident = {
      sequence: ++this.incidentSequence,
      atMs: Date.now(),
      code,
      detail,
    };
    this.incidents.push(incident);
    if (this.incidents.length > INCIDENT_LIMIT) this.incidents.shift();
    this.incidentCb?.(incident);
  }

  private markHealthy(): void {
    this.healthStatus = 'healthy';
    this.lastHealthyAtMs = Date.now();
  }

  private resetWatchdogBaselines(): void {
    this.lastWatchdogAt = performance.now();
    this.lastWatchdogVideoTime = this.video?.currentTime ?? 0;
    this.videoNoProgressMs = 0;
    this.silentForMs = 0;
    this.lastStemTimes.clear();
    this.stemNoProgressMs.clear();
    for (const c of this.channels.values()) {
      this.lastStemTimes.set(c.id, c.el.currentTime);
      this.stemNoProgressMs.set(c.id, 0);
    }
  }

  private bufferedAhead(el: HTMLMediaElement): number {
    const t = el.currentTime;
    for (let i = 0; i < el.buffered.length; i += 1) {
      if (el.buffered.start(i) <= t && el.buffered.end(i) >= t) {
        return Math.max(0, el.buffered.end(i) - t);
      }
    }
    return 0;
  }

  private describeError(error: unknown): string {
    if (error instanceof DOMException) return `${error.name}: ${error.message}`;
    if (error instanceof Error) return `${error.name}: ${error.message}`;
    return String(error);
  }

  private readonly handleVideoWaiting = (): void => {
    if (!this.desiredPlaying) return;
    this.recordIncident('video-buffering', `readyState=${this.video?.readyState ?? 'unknown'}`);
    this.healthStatus = 'recovering';
    // Hold the audible ensemble while leaving the master video's play/fetch
    // active. Pausing the video deprioritizes some browsers' Range loading;
    // TimeRanges and canplay alone do not prove the target frame is decodable.
    if (!this.recoveryInFlight) this.commandVersion += 1;
    this.videoBufferingForRecovery = true;
    for (const c of this.channels.values()) c.el.pause();
  };

  private readonly handleVideoStalled = (): void => {
    if (!this.desiredPlaying) return;
    // `stalled` only says the user agent is not currently receiving data; it
    // can fire while enough media remains buffered. Record it immediately and
    // let the clock/buffer sensors decide whether playback is actually broken.
    this.recordIncident('video-buffering', `network stalled; readyState=${this.video?.readyState ?? 'unknown'}`);
  };

  private readonly handleVideoPlaying = (): void => {
    if (!this.desiredPlaying) return;
    if (this.recoveryInFlight) return;
    if (this.videoBufferingForRecovery) {
      if (this.video && !this.videoReachedPendingSeek(this.video)) return;
      this.videoBufferingForRecovery = false;
      this.resetRecoveryCooldown();
      void this.recover('video-buffering');
      return;
    }
    if (Array.from(this.channels.values()).some((c) => c.el.paused)) {
      void this.recover('video-buffering');
      return;
    }
    if (this.video) this.hardSyncToVideo(this.video.currentTime, HARD_DRIFT_SEC);
  };

  private readonly handleVideoCanPlay = (): void => {
    // `canplay` is recorded by the browser but is deliberately not a restart
    // authority. Some Chromium paths emit it for a tiny/non-decodable range.
    // `playing` or direct clock advancement below owns coordinated resume.
  }

  private readonly handleVideoError = (): void => {
    const error = this.video?.error;
    this.recordIncident('video-media-error', `MediaError ${error?.code ?? 'unknown'} ${error?.message ?? ''}`.trim());
    this.healthStatus = 'failed';
  };

  private videoReachedPendingSeek(video: HTMLVideoElement): boolean {
    return this.pendingSeekTarget === null || (
      !video.seeking && Math.abs(video.currentTime - this.pendingSeekTarget) <= 0.25
    );
  }

  private attachVideoSensors(video: HTMLVideoElement): void {
    video.addEventListener('waiting', this.handleVideoWaiting);
    video.addEventListener('stalled', this.handleVideoStalled);
    video.addEventListener('playing', this.handleVideoPlaying);
    video.addEventListener('canplay', this.handleVideoCanPlay);
    video.addEventListener('error', this.handleVideoError);
  }

  private detachVideoSensors(video: HTMLVideoElement): void {
    video.removeEventListener('waiting', this.handleVideoWaiting);
    video.removeEventListener('stalled', this.handleVideoStalled);
    video.removeEventListener('playing', this.handleVideoPlaying);
    video.removeEventListener('canplay', this.handleVideoCanPlay);
    video.removeEventListener('error', this.handleVideoError);
  }

  /** Start a clean, independently attributable observation window per track. */
  private resetTrackSession(): void {
    this.desiredPlaying = false;
    this.commandVersion += 1;
    this.healthStatus = 'idle';
    this.stopSyncLoop();
    this.stopWatchdog();
    this.recoveryAttempts = 0;
    this.recoverySuccesses = 0;
    this.lastHealthyAtMs = null;
    this.recoveryInFlight = false;
    this.videoBufferingForRecovery = false;
    this.pendingSeekTarget = null;
    if (this.stemPrefetchTimer !== null) {
      window.clearTimeout(this.stemPrefetchTimer);
      this.stemPrefetchTimer = null;
    }
    this.lastRecoveryAt = 0;
    this.incidentSequence = 0;
    this.incidents = [];
  }

  private resetRecoveryCooldown(): void {
    this.lastRecoveryAt = 0;
    if (this.recoveryRetryTimer !== null) {
      window.clearTimeout(this.recoveryRetryTimer);
      this.recoveryRetryTimer = null;
    }
  }

  private releaseChannels(): void {
    for (const c of this.channels.values()) {
      // Set this before clearing src because the resulting error event may be
      // dispatched synchronously by some browser engines.
      c.released = true;
      c.el.pause();
      c.el.src = '';
      c.source.disconnect();
      c.gain.disconnect();
      c.analyser.disconnect();
    }
    this.channels.clear();
    this.stalledTicks = 0;
    this.resetWatchdogBaselines();
  }
}

/** Singleton engine instance (mirrors the salvaged audioManager singleton). */
export const playbackEngine: PlaybackEngine = new MediaElementEngine();
