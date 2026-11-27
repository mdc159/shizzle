/**
 * Shared handle for the transport button's user-gesture play/pause path.
 *
 * PlayerShell owns the real readiness gate (stems loaded + video staged) and
 * the handlePlay/handlePause callbacks from useAudioSync that actually drive
 * the media engine. It registers those here on every render so that other
 * user-gesture entry points — currently the Space keyboard shortcut in
 * App.tsx — can invoke the exact same path the transport button uses,
 * instead of only flipping Zustand store state.
 */

export interface TransportControlsHandle {
  /** Same gate the transport button uses to disable itself. */
  ready: boolean;
  playing: boolean;
  play: () => Promise<void>;
  pause: () => void;
}

let current: TransportControlsHandle | null = null;

export function registerTransportControls(handle: TransportControlsHandle | null): void {
  current = handle;
}

export function getTransportControls(): TransportControlsHandle | null {
  return current;
}
