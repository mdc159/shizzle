import { authFetch } from '@/lib/auth';
import type { PlaybackIncident, PlaybackMetrics } from './metrics';

export type PlaybackTelemetryEvent =
  | 'session-started'
  | 'playing'
  | 'heartbeat'
  | 'seek-started'
  | 'seek-settled'
  | 'pause'
  | 'ended'
  | 'replay'
  | 'visibility-hidden'
  | 'visibility-visible'
  | PlaybackIncident['code']
  | 'fatal'
  | 'session-ended';

interface EventPayload {
  sessionId: string;
  trackId: string;
  generation: number;
  sequence: number;
  event: PlaybackTelemetryEvent;
  clientAtMs: number;
  appBuild: string;
  browser: string;
  detail?: Record<string, unknown>;
}

interface QueuedEvent {
  payload: EventPayload;
  attempts: number;
}

// A seek storm can legitimately produce ~200 direct health events/minute
// (transport + buffering + recovery evidence). Keep enough durable backlog to
// ride out a temporary API/database interruption without discarding the very
// evidence needed to diagnose it.
const MAX_QUEUE = 1000;
const MAX_ATTEMPTS = 5;
const RETRY_MS = 2000;

function buildId(): string {
  const source = document.querySelector<HTMLScriptElement>('script[type="module"][src]')?.src;
  return source?.split('/').pop()?.slice(0, 128) || 'unknown';
}

/** Only first-party numeric/state evidence; never media URLs, cookies, or tokens. */
export function metricsDetail(metrics: PlaybackMetrics): Record<string, unknown> {
  const stemOffsets = Object.values(metrics.stems)
    .map((stem) => stem?.skewMs)
    .filter((value): value is number => typeof value === 'number');
  const interStemSkewMs = stemOffsets.length
    ? Math.max(...stemOffsets) - Math.min(...stemOffsets)
    : null;
  const maxStemVideoOffsetMs = stemOffsets.length
    ? Math.max(...stemOffsets.map((value) => Math.abs(value)))
    : null;
  return {
    desiredPlaying: metrics.desiredPlaying,
    health: metrics.health,
    output: metrics.output,
    video: metrics.video,
    stems: metrics.stems,
    limiter: metrics.limiter,
    interStemSkewMs,
    maxStemVideoOffsetMs,
  };
}

class PlaybackTelemetry {
  private sessionId: string | null = null;
  private trackId: string | null = null;
  private generation = 0;
  private sequence = 0;
  private queue: QueuedEvent[] = [];
  private flushing = false;
  private retryTimer: number | null = null;

  start(trackId: string, generation: number): void {
    if (this.trackId === trackId && this.generation === generation && this.sessionId) return;
    if (this.sessionId) this.send('session-ended', { reason: 'track-changed' });
    this.sessionId = crypto.randomUUID();
    this.trackId = trackId;
    this.generation = generation;
    this.sequence = 0;
    this.send('session-started');
  }

  send(event: PlaybackTelemetryEvent, detail?: Record<string, unknown>): void {
    if (!this.sessionId || !this.trackId || this.generation < 1) return;
    const payload: EventPayload = {
      sessionId: this.sessionId,
      trackId: this.trackId,
      generation: this.generation,
      sequence: ++this.sequence,
      event,
      clientAtMs: Date.now(),
      appBuild: buildId(),
      browser: navigator.userAgent.slice(0, 256),
      ...(detail ? { detail } : {}),
    };
    this.queue.push({ payload, attempts: 0 });
    if (this.queue.length > MAX_QUEUE) this.queue.shift();
    void this.flush();
  }

  incident(incident: PlaybackIncident, metrics: PlaybackMetrics): void {
    this.send(incident.code, {
      incidentSequence: incident.sequence,
      incidentAtMs: incident.atMs,
      detail: incident.detail.slice(0, 1000),
      ...metricsDetail(metrics),
    });
  }

  end(reason = 'unload'): void {
    if (!this.sessionId) return;
    this.send('session-ended', { reason });
    this.sessionId = null;
    this.trackId = null;
    this.generation = 0;
  }

  private async flush(): Promise<void> {
    if (this.flushing || this.queue.length === 0) return;
    this.flushing = true;
    try {
      while (this.queue.length > 0) {
        const current = this.queue[0];
        try {
          const response = await authFetch('/api/playback/telemetry', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(current.payload),
            keepalive: true,
          });
          if (response.ok || response.status === 409) {
            this.queue.shift();
            continue;
          }
          if (response.status === 429) {
            // Rate limiting is backpressure, not a bad event. Preserve the
            // head of the ordered queue and obey Retry-After without consuming
            // its finite transport-error retry budget.
            const retryAfterSeconds = Number(response.headers.get('Retry-After'));
            this.scheduleRetry(
              Number.isFinite(retryAfterSeconds) && retryAfterSeconds > 0
                ? retryAfterSeconds * 1000
                : RETRY_MS,
            );
            return;
          }
          if (response.status >= 400 && response.status < 500 && response.status !== 429) {
            // Invalid/private data must not loop forever.
            this.queue.shift();
            continue;
          }
          throw new Error(`telemetry HTTP ${response.status}`);
        } catch {
          current.attempts += 1;
          if (current.attempts >= MAX_ATTEMPTS) this.queue.shift();
          this.scheduleRetry();
          return;
        }
      }
    } finally {
      this.flushing = false;
    }
  }

  private scheduleRetry(delayMs = RETRY_MS): void {
    if (this.retryTimer !== null) return;
    this.retryTimer = window.setTimeout(() => {
      this.retryTimer = null;
      void this.flush();
    }, delayMs);
  }
}

export const playbackTelemetry = new PlaybackTelemetry();
