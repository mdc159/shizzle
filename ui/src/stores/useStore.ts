import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { Track, StemsManifest, JobStatus, StemId } from '@/types/karaoke';

// Re-export Track for backwards compatibility
export type { Track };

interface KaraokeState {
    // Player State
    playing: boolean;
    buffering: boolean;
    currentTime: number;
    duration: number;

    // UI State
    controlsVisible: boolean;
    activeDrawer: 'none' | 'library' | 'mixer' | 'source';

    // Data State
    currentTrack: Track | null;
    manifest: StemsManifest | null;
    activeJob: JobStatus | null;
    volume: number; // Master volume (0.0 to 1.0)
    stemGains: Record<StemId, number>; // dB scale (-60 to +12)
    stemMutes: Record<StemId, boolean>;
    stemSolos: Record<StemId, boolean>;

    // Actions
    setPlaying: (playing: boolean) => void;
    togglePlay: () => void;
    setControlsVisible: (visible: boolean) => void;
    setActiveDrawer: (drawer: 'none' | 'library' | 'mixer' | 'source') => void;
    loadTrack: (track: Track) => void;
    setManifest: (manifest: StemsManifest | null) => void;
    setActiveJob: (job: JobStatus | null) => void;
    setStemGain: (stem: StemId, value: number) => void;
    toggleStemMute: (stem: StemId) => void;
    toggleStemSolo: (stem: StemId) => void;
    setVolume: (val: number) => void;
    setCurrentTime: (time: number) => void;
    setDuration: (duration: number) => void;
}

export const useStore = create<KaraokeState>()(
    persist(
        (set) => ({
            // Player Defaults
            playing: false,
            buffering: false,
            currentTime: 0,
            duration: 0,

            // UI Defaults
            controlsVisible: true,
            activeDrawer: 'none',

            // Data Defaults
            currentTrack: null,
            manifest: null,
            activeJob: null,
            volume: 1.0,
            stemGains: {
                vocals: 0,
                drums: 0,
                bass: 0,
                guitar: 0,
                piano: 0,
                shizzle: 0,
            },
            stemMutes: {
                vocals: false,
                drums: false,
                bass: false,
                guitar: false,
                piano: false,
                shizzle: false,
            },
            stemSolos: {
                vocals: false,
                drums: false,
                bass: false,
                guitar: false,
                piano: false,
                shizzle: false,
            },

            // Actions
            setPlaying: (playing) => set({ playing }),
            togglePlay: () => set((state) => ({ playing: !state.playing })),

            setControlsVisible: (visible) => set({ controlsVisible: visible }),

            setActiveDrawer: (drawer) => set({ activeDrawer: drawer }),

            loadTrack: (track) => set({
                currentTrack: track,
                manifest: null, // Clear manifest, will be loaded by PlayerShell
                playing: false, // Don't auto-play until stems are loaded
                activeDrawer: 'none', // auto-close drawers on load
                currentTime: 0,
            }),

            setManifest: (manifest) => set({ manifest }),

            setActiveJob: (job) => set({ activeJob: job }),

            setStemGain: (stem, value) => set((state) => ({
                stemGains: { ...state.stemGains, [stem]: value }
            })),

            toggleStemMute: (stem) => set((state) => ({
                stemMutes: { ...state.stemMutes, [stem]: !state.stemMutes[stem] }
            })),

            toggleStemSolo: (stem) => set((state) => ({
                stemSolos: { ...state.stemSolos, [stem]: !state.stemSolos[stem] }
            })),

            setVolume: (val) => set({ volume: val }),
            setCurrentTime: (time) => set({ currentTime: time }),
            setDuration: (duration) => set({ duration }),
        }),
        {
            name: 'karaoke-storage', // unique name
            partialize: (state) => ({
                volume: state.volume,
                stemGains: state.stemGains,
                stemMutes: state.stemMutes,
                stemSolos: state.stemSolos,
            }), // persist only preferences
        }
    )
);
