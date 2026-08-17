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
import { getToken } from '@/lib/auth';
import type { StemId } from '@/types/karaoke';

type Command =
  | { type: 'mix'; stem: StemId; gainDb: number }
  | { type: 'mute'; stem: StemId; on: boolean }
  | { type: 'solo'; stem: StemId; on: boolean }
  | { type: 'master'; value: number };

interface StateSnapshot {
  type: 'state';
  track: string | null;
  gains: Record<StemId, number>;
  mutes: Record<StemId, boolean>;
  solos: Record<StemId, boolean>;
  master: number;
}

type RemoteMessage = Command | StateSnapshot;

const RECONNECT_BASE_MS = 1000;
const RECONNECT_MAX_MS = 8000;
const PUBLISH_THROTTLE_MS = 60;

function wsUrl(): string {
  const scheme = location.protocol === 'https:' ? 'wss' : 'ws';
  const token = getToken();
  const query = token ? `?token=${encodeURIComponent(token)}` : '';
  return `${scheme}://${location.host}/api/remote/ws${query}`;
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

    const send = (msg: RemoteMessage) => {
      if (socket?.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify(msg));
      }
    };

    // Coalesce rapid changes (fader drags) per key; flush on a short timer.
    const queue = (key: string, msg: RemoteMessage) => {
      pending.set(key, msg);
      if (publishTimer === undefined) {
        publishTimer = window.setTimeout(() => {
          publishTimer = undefined;
          pending.forEach((m) => send(m));
          pending.clear();
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
      };

      socket.onmessage = (event) => {
        let msg: RemoteMessage;
        try {
          msg = JSON.parse(event.data);
        } catch {
          return; // not ours
        }
        if (role === 'player' && msg.type !== 'state') applyCommand(msg);
        else if (role === 'remote' && msg.type === 'state') applyState(msg);
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
