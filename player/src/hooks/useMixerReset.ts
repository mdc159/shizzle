import { useStore } from '@/stores/useStore';
import type { StemId } from '@/types/karaoke';

/** Reset every strip to unity/unmuted/unsoloed. Shared by both mixer surfaces. */
export const useMixerReset = () => {
  const {
    stemGains, setStemGain,
    stemMutes, toggleStemMute,
    stemSolos, toggleStemSolo,
  } = useStore();
  const stems = Object.keys(stemGains) as StemId[];
  return () => {
    stems.forEach(stem => {
      setStemGain(stem, 0);
      if (stemMutes[stem]) toggleStemMute(stem);
      if (stemSolos[stem]) toggleStemSolo(stem);
    });
  };
};
