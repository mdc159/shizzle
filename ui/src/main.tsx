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

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
