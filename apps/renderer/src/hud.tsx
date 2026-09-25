import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { OverlayHUD } from './components/OverlayHUD.js';
import { connectHudStream } from './lib/hud-client.js';
import './index.css';

/** The OverlayHUD window's page (P3-13): one component, fed by its own preload. */

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element #root is missing from hud.html');
}

// Before the first render, as in `main.tsx`: MAIN replays the stream on page load.
connectHudStream();

createRoot(container).render(
  <StrictMode>
    <OverlayHUD />
  </StrictMode>,
);
