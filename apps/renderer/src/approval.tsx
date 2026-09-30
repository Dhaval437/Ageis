import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { ApprovalDialog } from './components/ApprovalDialog.js';
import { connectApprovalStream } from './lib/approval-client.js';
import './index.css';

/** The approval window's page (P3-19): the dialog, fed by its own preload. */

const container = document.getElementById('root');
if (!container) {
  throw new Error('Root element #root is missing from approval.html');
}

// Before the first render, as in `main.tsx`: MAIN replays the stream on page load.
connectApprovalStream();

createRoot(container).render(
  <StrictMode>
    <ApprovalDialog />
  </StrictMode>,
);
