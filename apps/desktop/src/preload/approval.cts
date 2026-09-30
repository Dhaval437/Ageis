/**
 * The approval window's preload (P3-19) — `window.aegisApproval`, and nothing else.
 *
 * The approval dialog is the one screen that can *allow* something, and it shows text
 * scraped off the user's screen, so its window gets two members: the stream, and an
 * answer to a question that is pending. MAIN checks every answer again
 * (`main/approval-policy.ts`) before it reaches the core (`AegisApprovalBridge`).
 *
 * `.cts` and self-contained for the same reasons as `bridge.cts`: a sandboxed preload
 * is CommonJS and cannot require a sibling file, so the channel names are literals,
 * and `tests/approval-bridge.test.ts` holds them equal to `main/approval-channels.ts`.
 */

import { contextBridge, ipcRenderer } from 'electron';
import type { IpcRendererEvent } from 'electron';
import type {
  AegisApprovalBridge,
  ApprovalChoice,
  BridgeResult,
  CoreResponse,
  CoreStreamMessage,
  RuleKind,
  Unsubscribe,
} from '@aegis/shared';

const approval: AegisApprovalBridge = {
  subscribe: (listener: (message: CoreStreamMessage) => void): Unsubscribe => {
    // The event is dropped: it carries a `sender` handle the page must not hold.
    const wrapped = (_event: IpcRendererEvent, message: CoreStreamMessage): void => {
      listener(message);
    };
    ipcRenderer.on('aegis:approval:event', wrapped);
    return () => {
      ipcRenderer.removeListener('aegis:approval:event', wrapped);
    };
  },
  answer: async (
    approvalId: number,
    choice: ApprovalChoice,
    rule: RuleKind | null,
  ): Promise<BridgeResult<CoreResponse>> =>
    (await ipcRenderer.invoke('aegis:approval:answer', {
      approvalId,
      choice,
      rule,
    })) as BridgeResult<CoreResponse>,
};

contextBridge.exposeInMainWorld('aegisApproval', approval);
