import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App.js';
import { connectStream } from './lib/stream-client.js';
import './index.css';

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element #root is missing from index.html');
}

// Before the first render, so the reset and replay MAIN sends on page load land.
connectStream();

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
