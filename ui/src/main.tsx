import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { playbackEngine } from '@/lib/playback/mediaElementEngine'
import { useStore } from '@/stores/useStore'

if (import.meta.env.DEV) {
  // Headless-test hook (Playwright): expose the real engine + store in dev
  // builds only, so probes assert on actual audio-graph state.
  (window as unknown as { __shizzle?: unknown }).__shizzle = {
    engine: playbackEngine,
    store: useStore,
  }
}

// Production-safe observability for browser control and field diagnosis. This
// exposes no credentials and no mutating controls—only direct engine metrics.
(window as unknown as { __shizzlePlaybackHealth?: unknown }).__shizzlePlaybackHealth = Object.freeze({
  getMetrics: () => playbackEngine.getMetrics(),
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
