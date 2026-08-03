/**
 * Stem Audio Manager
 * Handles Web Audio API for multi-stem karaoke playback
 */

import type { Stem, StemId } from '@/types/karaoke';

export class StemAudioManager {
  private audioContext: AudioContext | null = null;
  private sources: Map<StemId, MediaElementAudioSourceNode> = new Map();
  private gainNodes: Map<StemId, GainNode> = new Map();
  private audioElements: Map<StemId, HTMLAudioElement> = new Map();

  async initialize(): Promise<void> {
    if (!this.audioContext) {
      this.audioContext = new (window.AudioContext ||
        (window as unknown as { webkitAudioContext: typeof AudioContext }).webkitAudioContext)();
    }
    if (this.audioContext.state === 'suspended') {
      await this.audioContext.resume();
    }
  }

  async loadStems(basePath: string, stems: Stem[]): Promise<void> {
    await this.initialize();
    this.cleanup();

    const loadPromises = stems.map(async (stem) => {
      const audio = new Audio();
      audio.crossOrigin = 'anonymous';
      audio.preload = 'auto';
      audio.src = `${basePath}/${stem.file}`;

      // Create audio graph: source -> gain -> destination
      const source = this.audioContext!.createMediaElementSource(audio);
      const gain = this.audioContext!.createGain();
      gain.gain.value = stem.default_gain;

      source.connect(gain);
      gain.connect(this.audioContext!.destination);

      this.audioElements.set(stem.id, audio);
      this.sources.set(stem.id, source);
      this.gainNodes.set(stem.id, gain);

      // Wait for audio to be loadable (with 15s timeout)
      return new Promise<void>((resolve, reject) => {
        const timeout = setTimeout(() => {
          reject(new Error(`Stem load timeout: ${stem.file}`));
        }, 15000);
        audio.addEventListener('canplaythrough', () => { clearTimeout(timeout); resolve(); }, { once: true });
        audio.addEventListener('error', (e) => { clearTimeout(timeout); reject(e); }, { once: true });
        audio.load();
      });
    });

    await Promise.all(loadPromises);
  }

  setGain(stemId: StemId, value: number): void {
    const gain = this.gainNodes.get(stemId);
    if (gain) {
      // Smooth transition for gain changes
      gain.gain.setTargetAtTime(value, this.audioContext?.currentTime || 0, 0.05);
    }
  }

  getGain(stemId: StemId): number {
    const gain = this.gainNodes.get(stemId);
    return gain?.gain.value ?? 1;
  }

  syncToTime(time: number): void {
    for (const audio of this.audioElements.values()) {
      audio.currentTime = time;
    }
  }

  getAverageCurrentTime(): number {
    const elements = Array.from(this.audioElements.values());
    if (elements.length === 0) return 0;
    const total = elements.reduce((sum, audio) => sum + audio.currentTime, 0);
    return total / elements.length;
  }

  async play(): Promise<void> {
    await this.initialize();
    const results = await Promise.allSettled(
      Array.from(this.audioElements.values()).map((audio) => audio.play())
    );
    const failures = results.filter((r) => r.status === 'rejected');
    if (failures.length > 0) {
      this.pause();
      throw new Error(`${failures.length} stem(s) failed to play`);
    }
  }

  pause(): void {
    for (const audio of this.audioElements.values()) {
      audio.pause();
    }
  }

  seek(time: number): void {
    for (const audio of this.audioElements.values()) {
      audio.currentTime = time;
    }
  }

  setPlaybackRate(rate: number): void {
    for (const audio of this.audioElements.values()) {
      audio.playbackRate = rate;
    }
  }

  // Called externally to sync audio to video time
  syncToVideoTime(videoTime: number, thresholdSec = 0.1): void {
    for (const [id, audio] of this.audioElements.entries()) {
      const drift = Math.abs(audio.currentTime - videoTime);
      if (drift > thresholdSec) {
        // Hard correction only for significant drift.
        console.debug(`Syncing ${id}: drift was ${drift.toFixed(3)}s`);
        audio.currentTime = videoTime;
      }
    }
  }

  cleanup(): void {
    for (const audio of this.audioElements.values()) {
      audio.pause();
      audio.src = '';
    }
    this.audioElements.clear();
    this.sources.clear();
    this.gainNodes.clear();
  }

  // Convert linear gain (0-1) to dB (-∞ to 0)
  static linearToDb(value: number): number {
    if (value <= 0) return -Infinity;
    return 20 * Math.log10(value);
  }

  // Convert dB to linear gain
  static dbToLinear(db: number): number {
    if (db === -Infinity) return 0;
    return Math.pow(10, db / 20);
  }

  // Format dB for display
  static formatDb(value: number): string {
    const db = StemAudioManager.linearToDb(value);
    if (db === -Infinity) return '-∞';
    if (db >= 0) return `+${db.toFixed(1)}`;
    return db.toFixed(1);
  }
}

// Singleton instance
export const audioManager = new StemAudioManager();
