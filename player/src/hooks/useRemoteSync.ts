/**
 * Realtime mix sync over the /api/remote/ws relay (design spec §6).
 *
 * Two roles share one tiny JSON protocol:
 * - 'player'  — the browser doing the actual playback. Applies inbound
 *   mix/mute/solo/master commands through the store (useAudioSync then drives
 *   the engine exactly as for local moves) and publishes throttled `state`
 *   snapshots so remotes track reality.
 * - 'remote'  — a control surface (e.g. the iPad /remote page). Publishes its
 *   own store changes as commands and applies inbound `state` snapshots.
 *
 * The relay fans every frame out to all *other* clients and holds no state,
 * so both sides suppress re-publishing while applying an inbound frame to
 * avoid echo loops.
 */

import { useEffect, useRef, useState } from 'react';
import { useStore } from '@/stores/useStore';
import type { StemId } from '@/types/karaoke';

const STEMS: StemId[] = ['vocals', 'drums', 'bass', 'guitar', 'piano', 'shizzle'];

type Command =
  | { type: 'mix'; stem: StemId; gainDb: number }
  | { type: 'mute'; stem: StemId; on: boolean }
  | { type: 'solo'; stem: StemId; on: boolean }
  | { type: 'master'; value: number };

interface SyncRequest {
  type: 'sync-request';
}

interface StateSnapshot {
  type: 'state';
  track: string | null;
  gains: Record<StemId, number>;
  mutes: Record<StemId, boolean>;
  solos: Record<StemId, boolean>;
  master: number;
}

type RemoteMessage = Command | StateSnapshot | SyncRequest;

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 8000;
const PUBLISH_THROTTLE_MS = 60;

function wsUrl(): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  return `${scheme}://${location.host}/api/remote/ws`;
}

const isStem = (value: unknown): value is StemId =>
  typeof value === 'string' && STEMS.includes(value as StemId);

const isFiniteNumber = (value: unknown, min: number, max: number): value is number =>
  typeof value === 'number' && Number.isFinite(value) && value >= min && value <= max;

const isBooleanMap = (value: unknown): value is Record<StemId, boolean> =>
  typeof value === 'object' && value !== null &&
  STEMS.every((stem) => typeof (value as Record<string, unknown>)[stem] === 'boolean');

const isGainMap = (value: unknown): value is Record<StemId, number> =>
  typeof value === 'object' && value !== null &&
  STEMS.every((stem) => isFiniteNumber(
    (value as Record<string, unknown>)[stem], -60, 12
  ));

function parseMessage(data: unknown): RemoteMessage | null {
  if (typeof data !== 'string') return null;
  let value: unknown;
  try {
    value = JSON.parse(data);
  } catch {
    return null;
  }
  if (typeof value !== 'object' || value === null || !('type' in value)) return null;
  const msg = value as Record<string, unknown>;
  if (msg.type === 'sync-request') return { type: 'sync-request' };
  if (msg.type === 'mix' && isStem(msg.stem) && isFiniteNumber(msg.gainDb, -60, 12)) {
    return { type: 'mix', stem: msg.stem, gainDb: msg.gainDb };
  }
  if (msg.type === 'mute' && isStem(msg.stem) && typeof msg.on === 'boolean') {
    return { type: 'mute', stem: msg.stem, on: msg.on };
  }
  if (msg.type === 'solo' && isStem(msg.stem) && typeof msg.on === 'boolean') {
    return { type: 'solo', stem: msg.stem, on: msg.on };
  }
  if (msg.type === 'master' && isFiniteNumber(msg.value, 0, 1)) {
    return { type: 'master', value: msg.value };
  }
  if (
    msg.type === 'state' &&
    (msg.track === null || typeof msg.track === 'string') &&
    isGainMap(msg.gains) &&
    isBooleanMap(msg.mutes) &&
    isBooleanMap(msg.solos) &&
    isFiniteNumber(msg.master, 0, 1)
  ) {
    return {
      type: 'state', track: msg.track, gains: msg.gains,
      mutes: msg.mutes, solos: msg.solos, master: msg.master,
    };
  }
  return null;
}

export function useRemoteSync(role: 'player' | 'remote') {
  const [connected, setConnected] = useState(false);
  const [remoteTrackTitle, setRemoteTrackTitle] = useState<string | null>(null);

  const applyingInbound = useRef(false);
  const socketRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    let disposed = false;
    let socket: WebSocket | null = null;
    let reconnectDelay = RECONNECT_BASE_MS;
    let reconnectTimer: number | undefined;
    let publishTimer: number | undefined;
    const pending = new Map<string, RemoteMessage>();

    const send = (msg: RemoteMessage): boolean => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(msg));
        return true;
      }
      return false;
    };

    const flushPending = () => {
      pending.forEach((msg, key) => {
        if (send(msg)) pending.delete(key);
      });
    };

    // Coalesce rapid changes (fader drags) per key; flush on a short timer.
    const queue = (key: string, msg: RemoteMessage) => {
      pending.set(key, msg);
      if (publishTimer === undefined) {
        publishTimer = window.setTimeout(() => {
          publishTimer = undefined;
          flushPending();
        }, PUBLISH_THROTTLE_MS);
      }
    };

    const snapshot = (): StateSnapshot => {
      const s = useStore.getState();
      return {
        type: 'state',
        track: s.currentTrack ? s.currentTrack.title : null,
        gains: s.stemGains,
        mutes: s.stemMutes,
        solos: s.stemSolos,
        master: s.volume,
      };
    };

    const applyCommand = (msg: Command) => {
      const s = useStore.getState();
      applyingInbound.current = true;
      try {
        if (msg.type === 'mix') s.setStemGain(msg.stem, msg.gainDb);
        else if (msg.type === 'mute' && s.stemMutes[msg.stem] !== msg.on) s.toggleStemMute(msg.stem);
        else if (msg.type === 'solo' && s.stemSolos[msg.stem] !== msg.on) s.toggleStemSolo(msg.stem);
        else if (msg.type === 'master') s.setVolume(msg.value);
      } finally {
        applyingInbound.current = false;
      }
    };

    const applyState = (msg: StateSnapshot) => {
      const s = useStore.getState();
      applyingInbound.current = true;
      try {
        (Object.keys(msg.gains) as StemId[]).forEach((stem) => {
          if (s.stemGains[stem] !== msg.gains[stem]) s.setStemGain(stem, msg.gains[stem]);
          if (s.stemMutes[stem] !== msg.mutes[stem]) s.toggleStemMute(stem);
          if (s.stemSolos[stem] !== msg.solos[stem]) s.toggleStemSolo(stem);
        });
        if (s.volume !== msg.master) s.setVolume(msg.master);
      } finally {
        applyingInbound.current = false;
      }
      setRemoteTrackTitle(msg.track);
    };

    const connect = () => {
      if (disposed) return;
      socket = new WebSocket(wsUrl());
      socketRef.current = socket;

      socket.onopen = () => {
        reconnectDelay = RECONNECT_BASE_MS;
        setConnected(true);
        if (role === 'player') send(snapshot());
        else send({ type: 'sync-request' });
        flushPending();
      };

      socket.onmessage = (event) => {
        const msg = parseMessage(event.data);
        if (!msg) return;
        if (role === 'player') {
          if (msg.type === 'sync-request') send(snapshot());
          else if (msg.type !== 'state') {
            applyCommand(msg);
            // The player is authoritative. Re-publish after every accepted
            // remote command so all other remotes converge on the result.
            send(snapshot());
          }
        } else if (msg.type === 'state') applyState(msg);
      };

      socket.onclose = () => {
        setConnected(false);
        socketRef.current = null;
        if (!disposed) {
          reconnectTimer = window.setTimeout(connect, reconnectDelay);
          reconnectDelay = Math.min(reconnectDelay * 2, RECONNECT_MAX_MS);
        }
      };
      socket.onerror = () => socket?.close();
    };

    connect();

    // Publish local changes: the player broadcasts full snapshots; a remote
    // broadcasts per-control commands diffed against the previous state.
    let prev = useStore.getState();
    const unsubscribe = useStore.subscribe((state) => {
      const before = prev;
      prev = state;
      if (applyingInbound.current) return;

      if (role === 'player') {
        if (
          state.stemGains !== before.stemGains ||
          state.stemMutes !== before.stemMutes ||
          state.stemSolos !== before.stemSolos ||
          state.volume !== before.volume ||
          state.currentTrack !== before.currentTrack
        ) {
          queue('state', snapshot());
        }
        return;
      }

      (Object.keys(state.stemGains) as StemId[]).forEach((stem) => {
        if (state.stemGains[stem] !== before.stemGains[stem]) {
          queue(`mix:${stem}`, { type: 'mix', stem, gainDb: state.stemGains[stem] });
        }
        if (state.stemMutes[stem] !== before.stemMutes[stem]) {
          queue(`mute:${stem}`, { type: 'mute', stem, on: state.stemMutes[stem] });
        }
        if (state.stemSolos[stem] !== before.stemSolos[stem]) {
          queue(`solo:${stem}`, { type: 'solo', stem, on: state.stemSolos[stem] });
        }
      });
      if (state.volume !== before.volume) {
        queue('master', { type: 'master', value: state.volume });
      }
    });

    return () => {
      disposed = true;
      unsubscribe();
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer);
      if (publishTimer !== undefined) window.clearTimeout(publishTimer);
      socket?.close();
      socketRef.current = null;
    };
  }, [role]);

  return { connected, remoteTrackTitle };
}
