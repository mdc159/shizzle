// Stem definitions
export type StemId = 'vocals' | 'drums' | 'bass' | 'guitar' | 'piano' | 'shizzle';

export interface Stem {
  id: StemId;
  name: string;
  file: string;
  /** Default stem gain in dB (manifest v3). Convert via dbToLinear() at load. */
  default_gain_db: number;
}

export interface StemsManifest {
  version?: number;
  title: string;
  artist: string;
  duration: number;
  video: string;
  timeline?: {
    start_ms: number;
    duration_ms: number;
    sample_rate_hz: number;
  };
  video_meta?: {
    codec?: string;
    profile?: string;
    pix_fmt?: string;
    avg_frame_rate?: string;
    duration?: number;
  };
  sourceUrl?: string;
  videoId?: string;
  stems: Stem[];
}

export interface Track {
  id: string;
  title: string;
  artist: string;
  slug: string;
  duration: number;
  videoId?: string;
  thumbnailUrl?: string;
  publicUrl: string;
  status?: 'ready' | 'processing' | 'failed';
}

export interface LibraryResponse {
  tracks: Track[];
  total: number;
}

export interface JobStatus {
  jobId: string;
  status: 'pending' | 'extracting' | 'splitting' | 'encoding' | 'ready' | 'failed';
  progress?: number;
  message?: string;
  error?: string;
}

