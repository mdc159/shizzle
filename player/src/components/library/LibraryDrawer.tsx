import React, { useEffect, useState, useCallback } from 'react';
import { useStore } from '@/stores/useStore';
import { getLibrary } from '@/lib/api';
import type { Track } from '@/types/karaoke';
import {
  Sheet,
  SheetContent,
  SheetHeader,
  SheetTitle,
  SheetDescription
} from '@/components/ui/sheet';
import { Button } from '@/components/ui/button';
import { Loader2, Music, Play, RefreshCw, AlertCircle, Plus } from 'lucide-react';
import { toast } from 'sonner';

// Format duration as mm:ss
const formatDuration = (seconds: number): string => {
  const mins = Math.floor(seconds / 60);
  const secs = Math.floor(seconds % 60);
  return `${mins}:${secs.toString().padStart(2, '0')}`;
};

export const LibraryDrawer: React.FC = () => {
  const { activeDrawer, setActiveDrawer, loadTrack: playTrack } = useStore();
  const [tracks, setTracks] = useState<Track[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isOpen = activeDrawer === 'library';

  const fetchLibrary = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const data = await getLibrary();
      setTracks(data);
    } catch (err) {
      const message = err instanceof Error ? err.message : 'Failed to load library';
      setError(message);
      toast.error(message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (isOpen && tracks.length === 0 && !error) {
      fetchLibrary();
    }
  }, [isOpen, tracks.length, error, fetchLibrary]);

  const handleSelect = (track: Track) => {
    playTrack(track);
    // Drawer will auto-close via loadTrack action setting activeDrawer to 'none'
  };

  const handleRefresh = () => {
    fetchLibrary();
  };

  const handleAddSource = () => {
    setActiveDrawer('source');
  };

  return (
    <Sheet open={isOpen} onOpenChange={(open) => !open && setActiveDrawer('none')}>
      <SheetContent side="left" className="flex flex-col w-[400px] sm:w-[540px] sm:max-w-none border-r-zinc-800 bg-zinc-950 text-zinc-100">
        <SheetHeader>
          <div className="flex items-center justify-between">
            <SheetTitle className="text-zinc-100 text-2xl">Library</SheetTitle>
            <div className="flex items-center gap-2">
              <Button
                size="icon"
                variant="ghost"
                onClick={handleRefresh}
                disabled={loading}
                title="Refresh library"
              >
                <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
              </Button>
              <Button
                size="sm"
                variant="outline"
                onClick={handleAddSource}
                className="border-zinc-700 hover:bg-zinc-800"
              >
                <Plus className="h-4 w-4 mr-1" />
                Add
              </Button>
            </div>
          </div>
          <SheetDescription className="text-zinc-500">
            {tracks.length > 0
              ? `${tracks.length} track${tracks.length === 1 ? '' : 's'} available`
              : 'Select a track to start singing.'}
          </SheetDescription>
        </SheetHeader>

        <div className="mt-8 flex-1 min-h-0 overflow-y-auto space-y-4 pr-1">
          {loading ? (
            <div className="flex flex-col items-center justify-center p-8 gap-3">
              <Loader2 className="h-8 w-8 animate-spin text-zinc-500" />
              <p className="text-sm text-zinc-500">Loading library...</p>
            </div>
          ) : error ? (
            <div className="flex flex-col items-center justify-center p-8 gap-4">
              <AlertCircle className="h-8 w-8 text-red-400" />
              <p className="text-sm text-red-400">{error}</p>
              <Button variant="outline" size="sm" onClick={handleRefresh}>
                Try Again
              </Button>
            </div>
          ) : tracks.length === 0 ? (
            <div className="flex flex-col items-center justify-center p-8 gap-4 text-center">
              <Music className="h-12 w-12 text-zinc-700" />
              <div>
                <p className="text-zinc-400">No tracks yet</p>
                <p className="text-sm text-zinc-600 mt-1">
                  Upload a video file to get started
                </p>
              </div>
              <Button onClick={handleAddSource} className="mt-2">
                <Plus className="h-4 w-4 mr-2" />
                Add Your First Track
              </Button>
            </div>
          ) : (
            <div className="grid gap-2">
              {tracks.map(track => (
                <div
                  key={track.id}
                  className="group flex items-center justify-between p-3 rounded-lg hover:bg-zinc-900 transition-colors border border-transparent hover:border-zinc-800 cursor-pointer"
                  onClick={() => handleSelect(track)}
                >
                  <div className="flex items-center gap-4">
                    <div className="h-10 w-10 flex items-center justify-center rounded bg-zinc-900 group-hover:bg-zinc-800 text-zinc-600 group-hover:text-zinc-300">
                      <Music className="h-5 w-5" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <h4 className="font-medium text-zinc-200 truncate">{track.title}</h4>
                      <div className="flex items-center gap-2 text-sm text-zinc-500">
                        <span className="truncate">{track.artist}</span>
                        {track.duration > 0 && (
                          <>
                            <span className="text-zinc-700">•</span>
                            <span>{formatDuration(track.duration)}</span>
                          </>
                        )}
                      </div>
                    </div>
                  </div>

                  <Button
                    size="icon"
                    variant="ghost"
                    onClick={(e) => {
                      e.stopPropagation();
                      handleSelect(track);
                    }}
                    className="opacity-0 group-hover:opacity-100 transition-opacity"
                  >
                    <Play className="h-4 w-4" />
                  </Button>
                </div>
              ))}
            </div>
          )}
        </div>
      </SheetContent>
    </Sheet>
  );
};
