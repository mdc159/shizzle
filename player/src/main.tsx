import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'
import App from './App.tsx'
import { RemoteMixerPage } from '@/pages/RemoteMixerPage'
import { DashboardPage } from '@/pages/DashboardPage'
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

// Path-based entry: the control surface and dashboard are standalone pages
// (no router dependency; Caddy's SPA fallback serves index.html for both).
const path = location.pathname.replace(/\/+$/, '') || '/';
const Root = path === '/remote' ? RemoteMixerPage : path === '/dashboard' ? DashboardPage : App;

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <Root />
  </StrictMode>,
)
