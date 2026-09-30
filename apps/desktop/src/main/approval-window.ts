/**
 * The approval dialog's own window (`UI.md § 3`, § 5, P3-19): shown when the core asks
 * a question, hidden when none is left, over everything, and the one place Aegis
 * takes focus.
 *
 * Why each property:
 *
 * - **Its own window.** The main window may be hidden while the agent works — the HUD
 *   is all that shows — so a question must be able to surface on its own.
 * - **Always on top, and it asks for the focus.** Windows may refuse a background app
 *   the foreground (its foreground lock); then the window is still on top of
 *   everything, and the person clicks it. Nothing waits on getting the focus: the
 *   core's timer denies an unanswered question regardless.
 * - **It gives the focus back.** Hiding a window does not make Windows hand the
 *   focus on, and a hidden window holding it would swallow the person's typing — or
 *   the agent's. So it notes which window had the focus before it took it, and
 *   returns it there when it hides, if it still holds it.
 * - **Excluded from screen capture** (`setContentProtection`), as the HUD is. The
 *   model must never see, or ground a click on, the buttons that approve its own
 *   actions.
 * - **Made once, hidden, at startup.** Its first page load replays the stream, and a
 *   replay begins with a reset; made at question time, that reset would clear the
 *   very question that opened it. Made early, it is also ready the moment one comes.
 * - **Closing it denies** the question it shows (the oldest), rather than hiding a
 *   question the core is still waiting on. `Esc` in the page does the same.
 * - **Its own two-member preload** (`preload/approval.cts`); every answer is checked
 *   again here (`approval-policy.ts`) and its channels answer only its `webContents`.
 *
 * When it shows and what it accepts are decided in `approval-policy.ts`, which has no
 * Electron in it; this file is the window those decisions are carried out on.
 */

import { BrowserWindow, ipcMain, screen } from 'electron';
import type { IpcMainInvokeEvent } from 'electron';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import type { BridgeResult, CoreResponse, CoreStreamMessage } from '@aegis/shared';
import { APPROVAL_EVENT_CHANNEL, APPROVAL_INVOKE_CHANNELS } from './approval-channels.js';
import {
  createApprovalVisibility,
  createPendingApprovals,
  type ApprovalAnswer,
  type PendingApprovals,
} from './approval-policy.js';
import { isSenderTrusted } from './ipc.js';
import { resolveApprovalEntry, type RendererEntryInput } from './renderer-entry.js';
import type { ForegroundWindows, WindowHandle } from './win-input.js';
import { hardenNavigation } from './window.js';

const here = dirname(fileURLToPath(import.meta.url));
export const APPROVAL_PRELOAD_PATH = join(here, '..', 'preload', 'approval.cjs');

/**
 * `UI.md § 3`: 480 px wide. The height is the longest question's, measured in the
 * built app (a clipped prompt, a three-line reason and the open *Allow always* menu
 * come to 647 px), so no button is ever below the fold; a short one sits centred.
 * On a display too short for that (768 px at 150 %), the window takes the height
 * there is and the page scrolls.
 */
const WIDTH = 480;
const HEIGHT = 660;

/** `--surface-1` (dark) from `UI.md § 2`, which the page paints over once loaded. */
const BACKGROUND = '#12151a';

export interface ApprovalWindowOptions {
  readonly entry: RendererEntryInput;
  /** Send one checked answer to the core: `POST /v1/approvals/{id}`. */
  readonly answer: (answer: ApprovalAnswer) => Promise<BridgeResult<CoreResponse>>;
  /** The window's page (re)loaded: replay the stream to it, as for the others. */
  readonly onLoad?: () => void;
  /** Windows' foreground window (`win-input.ts`), or `null` where it cannot be had. */
  readonly foreground: ForegroundWindows | null;
  /** A monotonic clock in ms; `performance.now` by default. */
  readonly now?: () => number;
}

export interface ApprovalWindow {
  /** Feed every message MAIN forwards, in order: forwarded, and shown or hidden by. */
  readonly observe: (message: CoreStreamMessage) => void;
  /** Bring the window forward again if a question is pending; otherwise nothing. */
  readonly raise: () => void;
  readonly dispose: () => void;
}

const REFUSED: BridgeResult<CoreResponse> = {
  ok: false,
  error: { code: 'invalid_request', message: 'That request did not come from the dialog.' },
};

export function createApprovalWindow(options: ApprovalWindowOptions): ApprovalWindow {
  const pending: PendingApprovals = createPendingApprovals(
    options.now ?? (() => performance.now()),
  );
  let window: BrowserWindow | null = open();
  /** Who had the focus before the window last came up from hidden. */
  let previous: WindowHandle | null = null;
  const visibility = createApprovalVisibility({ show: surface, hide });

  function open(): BrowserWindow {
    const created = new BrowserWindow({
      width: WIDTH,
      height: HEIGHT,
      title: 'Aegis needs your approval',
      frame: false,
      resizable: false,
      maximizable: false,
      minimizable: false,
      fullscreenable: false,
      alwaysOnTop: true,
      show: false,
      backgroundColor: BACKGROUND,
      webPreferences: {
        preload: APPROVAL_PRELOAD_PATH,
        sandbox: true,
        contextIsolation: true,
        nodeIntegration: false,
        webviewTag: false,
        webSecurity: true,
      },
    });
    // Above full-screen apps and the taskbar, as the HUD is.
    created.setAlwaysOnTop(true, 'screen-saver');
    // `WDA_EXCLUDEFROMCAPTURE`: the model must never see its own approval buttons.
    created.setContentProtection(true);
    hardenNavigation(created);
    const entry = resolveApprovalEntry(options.entry);
    const load =
      entry.kind === 'url' ? created.loadURL(entry.value) : created.loadFile(entry.value);
    load.catch((error: unknown) => {
      console.error('[main] failed to load the approval window', error);
    });
    created.webContents.on('did-finish-load', () => {
      options.onLoad?.();
    });
    created.on('close', (event) => {
      // Only a person closes it (dispose destroys, which emits no `close`): deny.
      event.preventDefault();
      const shown = pending.ids()[0];
      if (shown !== undefined) {
        void options.answer({ approvalId: shown, choice: 'deny', rule: null });
      }
    });
    created.on('closed', () => {
      window = null;
    });
    return created;
  }

  function surface(): void {
    if (window === null || window.isDestroyed()) return;
    if (!window.isVisible()) {
      previous = options.foreground?.current() ?? null;
      // Sized each time it comes up: the display may have changed since the last one.
      const { workArea } = screen.getDisplayMatching(window.getBounds());
      window.setSize(WIDTH, Math.min(HEIGHT, workArea.height));
      window.center();
    }
    window.show();
    window.moveTop();
    // The one place Aegis asks for the focus. Windows may refuse it; the window is
    // on top either way.
    window.focus();
  }

  function hide(): void {
    if (window === null || window.isDestroyed()) return;
    // Only if the dialog still holds the focus: a person who has since clicked into
    // another window chose where they are.
    if (previous !== null && window.isFocused()) options.foreground?.restore(previous);
    previous = null;
    window.hide();
  }

  const isDialog = (sender: Electron.WebContents): boolean =>
    isSenderTrusted(
      sender.id,
      window !== null && !window.isDestroyed() ? window.webContents.id : null,
    );

  const onAnswer = async (
    event: IpcMainInvokeEvent,
    input: unknown,
  ): Promise<BridgeResult<CoreResponse>> => {
    if (!isDialog(event.sender)) return REFUSED;
    const checked = pending.check(input);
    if (!checked.ok) {
      return { ok: false, error: { code: 'invalid_request', message: checked.message } };
    }
    return options.answer(checked.answer);
  };
  ipcMain.handle(APPROVAL_INVOKE_CHANNELS.answer, onAnswer);

  return {
    observe: (message) => {
      if (window !== null && !window.isDestroyed()) {
        window.webContents.send(APPROVAL_EVENT_CHANNEL, message);
      }
      if (pending.observe(message)) visibility.update(pending.count(), message.kind === 'reset');
    },
    raise: () => {
      if (pending.count() > 0) surface();
    },
    dispose: () => {
      visibility.dispose();
      ipcMain.removeHandler(APPROVAL_INVOKE_CHANNELS.answer);
      if (window !== null && !window.isDestroyed()) window.destroy();
      window = null;
    },
  };
}
