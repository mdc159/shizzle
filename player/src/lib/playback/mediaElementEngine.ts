/**
 * Media-element PlaybackEngine with audio-master alignment.
 *
 * One HTMLAudioElement per stem, each routed:
 *   source -> DelayNode -> stem GainNode -> master GainNode -> limiter -> destination
 *
 * - Stem gains are dB end-to-end and split into two terms per channel: the
 *   manifest `default_gain_db` trim (captured at load, issue #25) and the
 *   user's store fader. The rendered gain is dbToLinear(trimDb + gainDb),
 *   so store sync and "Reset mixer" can never overwrite the manifest trim.
 *   Both are converted via dbToLinear().
 * - Master bus carries a fixed -3 dB headroom under the user volume because
 *   decoded AAC stems overshoot ~+1 dBFS at unity (spike 0.4); the
 *   DynamicsCompressorNode is configured as a conservative transparent
 *   limiter that catches only summing overshoot.
 * - Synchronization is audio-master alignment. The stems are the audible
 *   clock and are never seeked or rate-changed while playing: on WebKit
 *   (every iPad browser) a seek or a playbackRate change on a playing stem
 *   freezes its clock for about a third of a second, while a stem left alone
 *   holds a constant offset to the millisecond (iPad probe, 2026-10-02:
 *   correcting a playing stem was itself the dropout; ten corrections a
 *   second produced continuous cutouts). Instead each stem's DelayNode
 *   holds back how far its clock runs ahead of the latest-running stem, so
 *   all stems sound together without touching them, and the audio-less
 *   video — whose seeks land quickly even on WebKit — is moved to the
 *   stems' audible position. A stem's audible position is
 *   `el.currentTime - delaySec`; every sync comparison uses it.
 * - Start behaviour. A browser whose stems start together (desktop
 *   Chromium: within ~6–12 ms) is left exactly as it is — no delay, no gate,
 *   audio from the first sample. A device whose starts proved staggered
 *   remembers the delays it needed (localStorage, per stem order) and
 *   applies them before playback begins, so the stems are in line from the
 *   first sample; when playback restarts with enough audio before it
 *   (resume, scrub), it rolls in from two seconds earlier with the output
 *   silent and fades in over 250 ms finishing at the requested position, so
 *   the WebKit start freeze and alignment happen where nothing is audible.
 *   At the top of a song nothing is silenced: a brief hiccup beats losing
 *   the first notes.
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
  MAX_INTER_STEM_SKEW_SEC,
  STALL_TICKS_THRESHOLD,
  SYNC_INTERVAL_MS,
  VIDEO_ADVANCE_EPSILON_SEC,
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
const RENDER_SILENCE_DBFS = -90;
// A short digital-silence passage is valid music. Five seconds is long enough
// to catch a dead Web Audio graph without "repairing" ordinary rests.
const RENDER_SILENCE_MS = 5000;
const RECOVERY_COOLDOWN_MS = 1000;
// Alignment video-seek budget. Moving the video is the only while-playing
// correction, and it is silent and cheap, but a seek that does not hold must
// still not be repeated every watchdog tick: corrections are not judged, and
// none issued, while the video seek is landing or before it has settled; they
// back off while sync does not hold, and each aims ahead of the stems by what
// the previous one was measured to lose while landing.
const RESYNC_SETTLE_MS = 400;
const RESYNC_BACKOFF_MAX_MS = 5000;
/** Sync observed for this long after a correction means it held; losing sync
 *  after that is a new event, corrected at once. */
const RESYNC_HOLD_MS = 2000;
/** How long a mid-seek stem is excused from stall detection. Past this it is
 *  stalled at once, and recover() re-seeks the paused ensemble. */
const SEEK_LANDING_TIMEOUT_MS = 1500;
/** A video-seek lead can make up for any landing the video-stall rule will
 *  wait out. The watchdog only sees the window end on a tick, so a landing up
 *  to two ticks past it can still escape recovery and must be within reach of
 *  the lead. */
const MAX_VIDEO_SEEK_LEAD_SEC = (CLOCK_STALL_MS + 2 * WATCHDOG_INTERVAL_MS) / 1000;
/** After a start, user seek or recovery, stem clocks freeze briefly before
 *  running steadily (iPad probe, 2026-10-02: a start runs, freezes ~300 ms,
 *  then holds; each stem starts ~16 ms after the previous one). Offsets are
 *  not trusted until this much wall time has passed. */
const ALIGN_SETTLE_MS = 1200;
/** A browser whose stems start together has no freeze to wait out; only the
 *  stable-clock window stands between a restart and the first judgement, so
 *  a seek or recovery there settles about as fast as it did before alignment. */
const ALIGN_SETTLE_NATIVE_MS = 300;
/** A stem's offset is only meaningful once every stem clock has run at
 *  real-time speed over this window: a freezing stem's offset is still
 *  moving. */
const ALIGN_STABLE_WINDOW_MS = 500;
const ALIGN_STABLE_TOLERANCE_SEC = 0.02;
/** Longest delay a DelayNode may apply to line the stems up. */
const MAX_STEM_DELAY_SEC = 1;
/** Delay retargeting time constant: inaudible as a click, quick to settle. */
const DELAY_SMOOTHING_SEC = 0.03;
/** Retarget a delay only when the wanted value has moved this far; keeps a
 *  steady ensemble from nudging delays every watchdog tick. */
const DELAY_DEADBAND_SEC = 0.01;
/** Events from the video are ignored for at most this long after the
 *  alignment sought it: a seek that never lands must not mask a genuine
 *  video stall from the waiting/playing sensors (the watchdog's own
 *  no-progress rule still fires regardless). */
const VIDEO_SEEK_GRACE_MS = 2500;
/** Resume and scrub start this far before the requested position, silently,
 *  so the stems' start freeze and alignment are over when it arrives. */
const PREROLL_SEC = 2;
/** A position needs at least this much audio before it to roll in from
 *  earlier; at the top of a song the brief start hiccup is preferred to
 *  losing the first notes. */
const ROLLIN_MIN_POSITION_SEC = PREROLL_SEC + 0.5;
/** The roll-in gate opens by this timeout even if alignment was not
 *  reached: silence is never held longer than this. */
const START_GATE_MAX_MS = 3000;
/** Roll-in fade-in length; it finishes at the requested position, so
 *  nothing after that position is lost. */
const START_FADE_SEC = 0.25;
/** A moved delay needs this long to settle before the fade starts. */
const DELAY_SETTLE_MS = 150;
/** localStorage key holding the per-stem start delays this device last
 *  needed, by stem order. */
const START_DELAYS_KEY = 'shizzle_stem_start_delays';
const INCIDENT_LIMIT = 100;

/** The start delays this device last needed, by stem order; empty when
 *  nothing usable is stored. Storage may be unavailable (private mode,
 *  quota): the delays are then simply measured again on every start. */
function rememberedStartDelays(): number[] {
  try {
    const parsed: unknown = JSON.parse(localStorage.getItem(START_DELAYS_KEY) ?? '[]');
    return Array.isArray(parsed) &&
      parsed.every((value) => typeof value === 'number' && value >= 0 && value <= MAX_STEM_DELAY_SEC)
      ? (parsed as number[])
      : [];
  } catch {
    return [];
  }
}

interface StemChannel {
  id: StemId;
  el: HTMLAudioElement;
  source: MediaElementAudioSourceNode;
  /** Holds this stem's clock back to the latest-running stem (alignment). */
  delay: DelayNode;
  /** Current delay in seconds; the stem's audible position is
   *  el.currentTime - delaySec. */
  delaySec: number;
  gain: GainNode;
  analyser: AnalyserNode;
  analyserData: Float32Array<ArrayBuffer>;
  rmsDbfs: number | null;
  /** User fader in dB (store `stemGains`); starts at unity 0 on every load. */
  gainDb: number;
  /** Manifest `default_gain_db` trim in dB, re-captured from each manifest. */
  trimDb: number;
  muted: boolean;
  soloed: boolean;
  waitingEvents: number;
  stalledEvents: number;
  hardSeeks: number;
  /** performance.now() when this stem was first seen mid-seek, else null. */
  seekingSinceMs: number | null;
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
  /** performance.now() before which stem offsets are not trusted (a start,
   *  user seek or recovery recently froze the clocks). */
  private alignReadyAtMs = 0;
  /** Recent stem clock samples for the real-time stability window. */
  private alignHistory: Array<{ atMs: number; stemTimes: number[] }> = [];
  /** How far a playing video lands behind its aim, as last measured. */
  private videoLeadSec = 0;
  /** The last alignment video seek, until its landing has been measured. */
  private videoFlight: { leadSec: number; landedAtMs: number | null } | null = null;
  /** performance.now() when the alignment last sought the video, else null.
   *  While that seek is in flight, its own waiting/playing events are not
   *  buffering incidents. */
  private videoAlignSeekSinceMs: number | null = null;
  /** Alignment seeks of the video since load (metrics/telemetry). */
  private videoSeeks = 0;
  /** True while the roll-in holds the master bus silent. */
  private outputGated = false;
  /** Audible position at which the roll-in's fade must finish; null when no
   *  roll-in is pending (the gate, if any, opens as soon as aligned). */
  private gateOpenAt: number | null = null;
  /** Failsafe opener for the roll-in gate. */
  private gateTimer: number | null = null;
  private lastResyncAt = 0;
  /** Corrections issued since sync last held for RESYNC_HOLD_MS. */
  private resyncStreak = 0;
  /** performance.now() when sync was first observed since the last correction. */
  private syncHeldSince: number | null = null;
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
      const delay = this.ctx!.createDelay(MAX_STEM_DELAY_SEC);
      const gain = this.ctx!.createGain();
      const analyser = this.ctx!.createAnalyser();
      analyser.fftSize = 512;
      analyser.smoothingTimeConstant = 0.1;
      const analyserData = new Float32Array(analyser.fftSize);
      // Contract fix (Phase 1.3): manifest gain is dB and converted here.
      // The legacy code assigned the raw value as a linear gain, so the
      // written `0` (meaning 0 dB) silenced every stem.
      // Issue #25: the trim is a separate per-channel term from the user
      // fader, which starts at unity and is forwarded by useAudioSync.
      const trimDb = stem.default_gain_db ?? 0;
      gain.gain.value = dbToLinear(trimDb);

      source.connect(delay);
      delay.connect(gain);
      gain.connect(analyser);
      analyser.connect(this.masterGain!);
      // A device whose starts proved staggered remembers the delays it
      // needed; applying them before playback begins puts the stems in line
      // from the first sample instead of after the alignment's settle window.
      const startDelaySec = rememberedStartDelays()[this.channels.size] ?? 0;
      delay.delayTime.value = startDelaySec;

      const channel: StemChannel = {
        id: stem.id,
        el,
        source,
        delay,
        delaySec: startDelaySec,
        gain,
        analyser,
        analyserData,
        rmsDbfs: null,
        gainDb: 0,
        trimDb,
        muted: false,
        soloed: false,
        waitingEvents: 0,
        stalledEvents: 0,
        hardSeeks: 0,
        seekingSinceMs: null,
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
      if (this.startNeedsAligning() && video.currentTime >= ROLLIN_MIN_POSITION_SEC) {
        // Resume with room to roll in from: start silently two seconds
        // earlier, let the stems start, freeze and be aligned during the
        // preroll, and fade in finishing at the requested position.
        this.gateOpenAt = video.currentTime;
        const from = video.currentTime - PREROLL_SEC;
        this.videoAlignSeekSinceMs = performance.now();
        video.currentTime = from;
        this.applyStartPattern(from);
        this.lastVideoTime = from;
      } else {
        this.gateOpenAt = null;
        if (this.outputGated) this.openOutput();
        // Startup alignment: put every stem at the same raw position with
        // the remembered start delays (a paused seek is cheap everywhere,
        // 21 ms for all six on the iPad), so the delays never accumulate
        // across restarts.
        this.applyStartPattern(video.currentTime);
        this.lastVideoTime = video.currentTime;
      }
    }
    this.stalledTicks = 0;
    this.resetResyncBudget();
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
    // A pending roll-in loses its failsafe opener; the resume decides
    // whether to roll in again or open at once.
    if (this.gateTimer !== null) {
      window.clearTimeout(this.gateTimer);
      this.gateTimer = null;
    }
    for (const c of this.channels.values()) {
      c.el.pause();
    }
  }

  seek(t: number): void {
    // The app's seek reconciliation (PlayerShell re-issues the store's time
    // when the video is more than a second from it) hears the roll-in's
    // two-second rewind as exactly that. A seek to the position a roll-in
    // is already opening at is that echo, not a user action: re-assert the
    // preroll position (the echo rewound the video to the target) and keep
    // rolling.
    if (this.desiredPlaying && this.gateOpenAt !== null && Math.abs(t - this.gateOpenAt) < 0.05) {
      if (this.video) this.video.currentTime = this.gateOpenAt - PREROLL_SEC;
      return;
    }
    // A user seek starts a new recovery window. Reusing the cooldown from the
    // previous seek can add almost a full second and violate the 3 s settled
    // acceptance gate during rapid scrubbing.
    this.resetRecoveryCooldown();
    if (this.desiredPlaying && this.startNeedsAligning() && t >= ROLLIN_MIN_POSITION_SEC) {
      // Scrub with room to roll in from: as the resume path above.
      this.gateOpenAt = t;
      t -= PREROLL_SEC;
      this.videoAlignSeekSinceMs = performance.now();
      if (this.video) this.video.currentTime = t;
    } else {
      this.gateOpenAt = null;
      // A seek that cannot roll in (top of song, or a device whose stems
      // start together) must never leave the output silent.
      if (this.outputGated) this.openOutput();
    }
    this.resetResyncBudget();
    if (this.desiredPlaying) {
      // The video is the authoritative clock and its target frame gates every
      // audible decoder. Do not launch seven competing Range seeks at once:
      // let the video fetch/advance first, then the paused prefetch re-bases
      // all six stems together at the observed master time.
      this.healthStatus = 'recovering';
      this.videoBufferingForRecovery = true;
      this.pendingSeekTarget = t;
      for (const c of this.channels.values()) c.el.pause();
      if (this.stemPrefetchTimer !== null) window.clearTimeout(this.stemPrefetchTimer);
      this.stemPrefetchTimer = window.setTimeout(() => {
        this.stemPrefetchTimer = null;
        if (!this.desiredPlaying || this.pendingSeekTarget !== t) return;
        this.applyStartPattern(this.video ? this.video.currentTime : t);
      }, MASTER_SEEK_HEAD_START_MS);
    } else {
      this.pendingSeekTarget = null;
      this.applyStartPattern(t);
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
    // While the roll-in gate holds the output silent, a volume change must
    // not open it; the stored level is what the fade-in ramps to.
    if (this.ctx && this.masterGain && !this.outputGated) {
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
    const anySoloed = Array.from(this.channels.values()).some((c) => c.soloed);
    for (const c of this.channels.values()) {
      // gainLinear reports the user fader only (0 when silenced); the
      // rendered node gain is dbToLinear(c.trimDb + c.gainDb) otherwise.
      const silenced = c.muted || (anySoloed && !c.soloed);
      stems[c.id] = {
        // Audible offset: the element clock minus the delay currently holding
        // this stem back, against the video clock.
        skewMs: vt === null ? null : Math.round((c.el.currentTime - c.delaySec - vt) * 1000),
        readyState: c.el.readyState,
        waitingEvents: c.waitingEvents,
        stalledEvents: c.stalledEvents,
        playbackRate: c.el.playbackRate,
        hardSeeks: c.hardSeeks,
        delayMs: Math.round(c.delaySec * 1000),
        gainLinear: silenced ? 0 : dbToLinear(c.gainDb),
        trimDb: c.trimDb,
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
      videoSeeks: this.videoSeeks,
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
      // Issue #25: rendered gain combines the manifest trim with the user
      // fader (dbToLinear(trimDb + gainDb)); neither term overwrites the other.
      const silenced = c.muted || (anySoloed && !c.soloed);
      const target = silenced ? 0 : dbToLinear(c.trimDb + c.gainDb);
      c.gain.gain.setTargetAtTime(target, now, GAIN_SMOOTHING_SEC);
    }
  }

  /** A stem's audible position: its element clock minus the delay currently
   *  holding it back. Every sync comparison uses this, never the raw clock,
   *  so an aligned ensemble never looks split by its own delays. */
  private audibleTime(c: StemChannel): number {
    return c.el.currentTime - c.delaySec;
  }

  private hardSyncToVideo(videoTime: number, thresholdSec: number): void {
    for (const c of this.channels.values()) {
      const drift = Math.abs(this.audibleTime(c) - videoTime);
      if (drift > thresholdSec) {
        console.debug(`Syncing ${c.id}: drift was ${drift.toFixed(3)}s`);
        // Seek to the target plus this stem's delay so the audible position
        // lands on the target.
        c.el.currentTime = videoTime + c.delaySec;
        c.hardSeeks += 1;
        // This seek moves the ensemble too, so a pending landing measurement
        // would credit its result to the wrong correction.
        this.videoFlight = null;
      }
    }
  }

  /**
   * True while this stem's seek is mid-flight and still inside its landing
   * window. Until `seeked`, a media element reports the seek target as
   * currentTime, so a frozen clock is expected and is not a stall.
   */
  private seekLanding(c: StemChannel, now: number): boolean {
    if (!c.el.seeking) {
      c.seekingSinceMs = null;
      return false;
    }
    c.seekingSinceMs ??= now;
    return now - c.seekingSinceMs < SEEK_LANDING_TIMEOUT_MS;
  }

  /**
   * True on a device whose stems have started out of line before: a
   * remembered start delay reached the audible spread threshold.
   */
  private startNeedsAligning(): boolean {
    return rememberedStartDelays().some((delay) => delay >= MAX_INTER_STEM_SKEW_SEC);
  }

  /**
   * Re-base a paused ensemble for a restart: every stem at the same raw
   * position, each DelayNode at this device's remembered start delay (0
   * where nothing is remembered). The known start stagger is then
   * pre-compensated — the stems are in line from the first sample — and the
   * alignment step only trims the difference between this start and the
   * remembered pattern, so delays never accumulate across restarts. Seeks
   * issued on paused elements land at once on every browser measured.
   */
  private applyStartPattern(position: number): void {
    const remembered = rememberedStartDelays();
    let i = 0;
    for (const c of this.channels.values()) {
      if (Math.abs(c.el.currentTime - position) >= 0.005) {
        c.el.currentTime = position;
        c.hardSeeks += 1;
      }
      const delaySec = Math.min(MAX_STEM_DELAY_SEC, remembered[i] ?? 0);
      i += 1;
      if (Math.abs(delaySec - c.delaySec) < 0.0005) continue;
      c.delaySec = delaySec;
      c.delay.delayTime.value = delaySec;
    }
  }

  /** Hold the master bus silent while a roll-in's stems start, freeze and
   *  get aligned; a failsafe timer opens it regardless after
   *  START_GATE_MAX_MS. */
  private gateOutput(): void {
    if (!this.ctx || !this.masterGain) return;
    this.outputGated = true;
    const gain = this.masterGain.gain;
    gain.cancelScheduledValues(this.ctx.currentTime);
    gain.setValueAtTime(0, this.ctx.currentTime);
    if (this.gateTimer !== null) window.clearTimeout(this.gateTimer);
    this.gateTimer = window.setTimeout(() => this.openOutput(), START_GATE_MAX_MS);
  }

  /** Open the roll-in gate, now or after delayMs, fading in over
   *  START_FADE_SEC to the user's level. */
  private openOutput(delayMs = 0): void {
    if (!this.outputGated || !this.ctx || !this.masterGain) return;
    if (delayMs > 0) {
      if (this.gateTimer !== null) window.clearTimeout(this.gateTimer);
      this.gateTimer = window.setTimeout(() => this.openOutput(), delayMs);
      return;
    }
    this.outputGated = false;
    this.gateOpenAt = null;
    if (this.gateTimer !== null) {
      window.clearTimeout(this.gateTimer);
      this.gateTimer = null;
    }
    const gain = this.masterGain.gain;
    const at = this.ctx.currentTime;
    gain.cancelScheduledValues(at);
    gain.setValueAtTime(0, at);
    gain.linearRampToValueAtTime(dbToLinear(this.masterGainDb + MASTER_HEADROOM_DB), at + START_FADE_SEC);
  }

  /** Drop all roll-in state and restore the user's level at once (track
   *  change, teardown): the next start must not inherit a silent bus. */
  private clearOutputGate(): void {
    if (this.gateTimer !== null) {
      window.clearTimeout(this.gateTimer);
      this.gateTimer = null;
    }
    this.outputGated = false;
    this.gateOpenAt = null;
    if (this.ctx && this.masterGain) {
      const at = this.ctx.currentTime;
      this.masterGain.gain.cancelScheduledValues(at);
      this.masterGain.gain.setValueAtTime(dbToLinear(this.masterGainDb + MASTER_HEADROOM_DB), at);
    }
  }

  /**
   * Start waiting again before stem offsets are trusted: a start, user seek
   * or recovery recently froze the clocks. The learned video lead is kept: it
   * describes how this browser lands video seeks on this element, which none
   * of those events change. A roll-in still pending through a recovery is
   * re-armed so the recovery cannot leave the output silent.
   */
  private restartAlignment(): void {
    this.alignReadyAtMs =
      performance.now() + (this.startNeedsAligning() ? ALIGN_SETTLE_MS : ALIGN_SETTLE_NATIVE_MS);
    this.alignHistory = [];
    this.videoFlight = null;
    this.videoAlignSeekSinceMs = null;
    if (this.desiredPlaying && this.gateOpenAt !== null) this.gateOutput();
  }

  /**
   * True while the alignment's own video seek is in flight (or has failed to
   * land within the grace window). Its waiting/playing events are the seek
   * itself, not a buffering incident; a genuine stall that outlives the
   * window — or stops the clock — is still caught by the watchdog's own
   * no-progress rule.
   */
  private alignmentVideoSeekActive(): boolean {
    const since = this.videoAlignSeekSinceMs;
    if (since === null) return false;
    if (!this.video || !this.video.seeking || performance.now() - since >= VIDEO_SEEK_GRACE_MS) {
      this.videoAlignSeekSinceMs = null;
      return false;
    }
    return true;
  }

  /**
   * One audio-master alignment step, on the watchdog cadence. Lines the stems
   * up with their DelayNodes and moves the silent video to their audible
   * position; never seeks or rate-changes a playing stem. Returns true when
   * stems and video are in sync.
   */
  private alignTick(video: HTMLVideoElement, now: number): boolean {
    const channels = Array.from(this.channels.values());
    if (channels.length === 0) return true;
    const stemTimes = channels.map((c) => c.el.currentTime);
    this.alignHistory.push({ atMs: now, stemTimes });
    while (this.alignHistory.length > 0 && now - this.alignHistory[0].atMs > 2 * ALIGN_STABLE_WINDOW_MS) {
      this.alignHistory.shift();
    }
    if (now < this.alignReadyAtMs || channels.some((c) => c.el.seeking)) return false;
    // Every stem clock must have run at real-time speed over the window: a
    // freezing stem's offset is still moving and says nothing yet.
    const then = this.alignHistory.find((h) => now - h.atMs <= ALIGN_STABLE_WINDOW_MS + 60);
    if (!then || now - then.atMs < ALIGN_STABLE_WINDOW_MS - 150) return false;
    const wall = (now - then.atMs) / 1000;
    if (
      stemTimes.some((time, i) => Math.abs(time - then.stemTimes[i] - wall) > ALIGN_STABLE_TOLERANCE_SEC)
    ) {
      return false;
    }

    // Stems: delay each one by how far its clock runs ahead of the
    // latest-running stem. The latest one keeps delay 0. A browser whose
    // stems started together (desktop Chromium: within ~6-12 ms) is left
    // exactly as it started — no delay is applied and nothing is stored —
    // unless a delay is already in use on this device.
    const latest = Math.min(...stemTimes);
    const aligning =
      channels.some((c) => c.delaySec > 0) ||
      Math.max(...stemTimes) - latest >= MAX_INTER_STEM_SKEW_SEC;
    let delaysMoved = false;
    channels.forEach((c, i) => {
      if (!aligning) return;
      const wanted = Math.min(MAX_STEM_DELAY_SEC, Math.max(0, stemTimes[i] - latest));
      if (Math.abs(wanted - c.delaySec) <= DELAY_DEADBAND_SEC) return;
      console.debug(`Delaying ${c.id} by ${(wanted * 1000).toFixed(0)} ms`);
      c.delaySec = wanted;
      c.delay.delayTime.setTargetAtTime(wanted, this.ctx!.currentTime, DELAY_SMOOTHING_SEC);
      delaysMoved = true;
    });
    if (delaysMoved) {
      // Remember what this device needed so the next load applies it before
      // playback begins; storage being unavailable just means measuring
      // again next time.
      try {
        localStorage.setItem(
          START_DELAYS_KEY,
          JSON.stringify(channels.map((c) => Math.round(c.delaySec * 1000) / 1000)),
        );
      } catch {
        // Measured again on the next start.
      }
    }
    // The stems are what is heard: once their delays are in place the output
    // can fade in, whatever the video still has to do. The fade is scheduled
    // to finish at the roll-in's requested position.
    const untilTargetMs =
      this.gateOpenAt === null ? 0 : (this.gateOpenAt - latest) * 1000 - START_FADE_SEC * 1000;
    this.openOutput(Math.max(delaysMoved ? DELAY_SETTLE_MS : 0, untilTargetMs));
    if (delaysMoved) return false;

    // Video: move it to the stems' audible position (they all share the
    // latest stem's time now), aimed ahead by what the previous seek was
    // measured to lose while landing.
    if (video.seeking) return false;
    if (this.videoFlight) {
      this.videoFlight.landedAtMs ??= now;
      if (now - this.videoFlight.landedAtMs < RESYNC_SETTLE_MS) return false;
      const residual = video.currentTime - latest;
      this.videoLeadSec = Math.max(
        0,
        Math.min(MAX_VIDEO_SEEK_LEAD_SEC, this.videoFlight.leadSec - residual),
      );
      this.videoFlight = null;
    }
    const offset = video.currentTime - latest;
    // Audible stem-to-stem separation beyond the threshold means the delay
    // cap was hit; that is not sync either.
    const audibleSpread = Math.max(...channels.map((c) => this.audibleTime(c))) - latest;
    if (Math.abs(offset) < HARD_DRIFT_SEC && audibleSpread < HARD_DRIFT_SEC) return true;
    const held = this.syncHeldSince !== null && now - this.syncHeldSince >= RESYNC_HOLD_MS;
    this.syncHeldSince = null;
    if (this.lastResyncAt === 0 || held) {
      this.resyncStreak = 0;
    } else {
      // Sync has not held since the last correction: each further one waits
      // twice as long, so a browser this cannot converge on hears a seek
      // every few seconds, not ten a second.
      const spacingMs = Math.min(
        RESYNC_SETTLE_MS * 2 ** (this.resyncStreak - 1),
        RESYNC_BACKOFF_MAX_MS,
      );
      if (now - this.lastResyncAt < spacingMs) return false;
    }
    console.debug(
      `Moving video by ${(-offset * 1000).toFixed(0)} ms (lead ${(this.videoLeadSec * 1000).toFixed(0)} ms)`,
    );
    video.currentTime = latest + this.videoLeadSec;
    this.videoFlight = { leadSec: this.videoLeadSec, landedAtMs: null };
    this.videoAlignSeekSinceMs = now;
    this.videoSeeks += 1;
    this.lastResyncAt = now;
    this.resyncStreak += 1;
    // The video clock just jumped; re-arm the direct stall sensors so the
    // seek is not read as the clock failing to advance.
    this.resetWatchdogBaselines();
    return false;
  }

  /**
   * Start a fresh budget. The learned video lead is kept: it describes how
   * this browser lands video seeks on this element, which a pause, a user
   * seek or a recovery does not change. A new track resets it with its
   * elements.
   */
  private resetResyncBudget(): void {
    this.restartAlignment();
    this.lastResyncAt = 0;
    this.resyncStreak = 0;
    this.syncHeldSince = null;
    // The watchdog may not have been running to see the last seek finish.
    for (const c of this.channels.values()) c.seekingSinceMs = null;
  }

  private averageCurrentTime(): number {
    const list = Array.from(this.channels.values());
    if (list.length === 0) return 0;
    return list.reduce((sum, c) => sum + this.audibleTime(c), 0) / list.length;
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

  /** One stall-policy evaluation on the 1 s cadence. While-playing sync is
   *  the alignment step on the watchdog cadence; this loop only watches the
   *  video clock itself. */
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
      // A landing seek holds currentTime at its target. That is not a stalled
      // decoder, and recovering from it would seek on top of the seek.
      const landing = this.seekLanding(c, now);
      // Past its landing window a mid-seek stem is stalled without further
      // waiting: the lead cannot make up for a landing that long, so the
      // paused re-seek in recover() is the only correction left.
      const overdue = c.el.seeking && !landing;
      const stalledFor = advanced || landing ? 0 : (this.stemNoProgressMs.get(c.id) ?? 0) + elapsed;
      this.lastStemTimes.set(c.id, c.el.currentTime);
      this.stemNoProgressMs.set(c.id, stalledFor);
      if (c.el.error) {
        this.healthStatus = 'failed';
        return;
      }
      if ((c.el.paused || overdue || stalledFor >= CLOCK_STALL_MS) && videoAdvanced) {
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
      if (!this.alignTick(video, now)) {
        // Health includes synchronization; while the alignment has not yet
        // lined the stems up and moved the video to them, it has not settled.
        if (this.healthStatus === 'healthy') this.healthStatus = 'starting';
        return;
      }
      this.syncHeldSince ??= now;
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
    // The roll-in gate silences the bus on purpose; that is not a dead graph.
    this.silentForMs =
      this.inputSignalPresent && !outputPresent && !this.outputGated
        ? this.silentForMs + elapsedMs
        : 0;
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
      // A stem still mid-seek past its landing window cannot be corrected
      // while playing: another seek would restart it and it would land late
      // again. Hold the whole ensemble and re-seek it paused, as for a
      // buffering video; a stem that cannot become ready fails the recovery.
      const stemMidSeek =
        reason === 'stem-clock-stalled' && Array.from(this.channels.values()).some((c) => c.el.seeking);
      const coordinatedSeek = Boolean(
        this.video && ((reason === 'video-buffering' && this.lastHealthyAtMs !== null) || stemMidSeek),
      );
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
        }
        // Re-base the paused ensemble with the remembered start delays: the
        // stems come back in line, so no seek of a playing stem is needed.
        this.applyStartPattern(target);
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
      // No hard sync of the resumed ensemble: with the remembered delays
      // applied at the paused re-seek the stems are back in line, and a
      // seek issued here would land on playing stems. Any residue is the
      // alignment's, which moves only the video.
      if (this.video?.paused || Array.from(this.channels.values()).some((c) => c.el.paused)) {
        throw new Error('A media element remained paused after recovery');
      }
      this.recoverySuccesses += 1;
      this.pendingSeekTarget = null;
      this.videoBufferingForRecovery = false;
      this.resetResyncBudget();
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
    // The alignment's own video seek is not a buffering incident: answering
    // it would pause every stem for a correction the engine just made.
    if (this.alignmentVideoSeekActive()) return;
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
    // The alignment's own video seek landing is not a buffering recovery.
    if (this.alignmentVideoSeekActive()) return;
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
    // No hard sync here: a playing stem is never seeked. If the video has
    // drifted from the stems' audible position, the alignment step moves
    // the video on its next watchdog tick.
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
    this.resetResyncBudget();
    this.videoLeadSec = 0;
    this.videoSeeks = 0;
    this.clearOutputGate();
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
      c.delay.disconnect();
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
