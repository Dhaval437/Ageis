/**
 * The tray menu template (`UI.md § 3`: status, kill switch, show/hide, quit).
 *
 * Pure and `electron`-free at runtime — the only import is a type, which is
 * erased — so the menu's shape is unit-testable without an Electron process.
 */

import type { MenuItemConstructorOptions } from 'electron';

/** The kill-switch accelerator from `UI.md § 4`. Registered for real in P3-06. */
export const KILL_SWITCH_ACCELERATOR = 'Control+Alt+Shift+Q';

export interface TrayMenuState {
  readonly windowVisible: boolean;
  readonly taskRunning: boolean;
}

export interface TrayMenuActions {
  readonly toggleWindow: () => void;
  readonly stopAgent: () => void;
  readonly quit: () => void;
}

export function buildTrayMenuTemplate(
  state: TrayMenuState,
  actions: TrayMenuActions,
): MenuItemConstructorOptions[] {
  return [
    { label: state.taskRunning ? 'Aegis — running' : 'Aegis — idle', enabled: false },
    { type: 'separator' },
    {
      // Disabled while nothing is running: there is no agent to stop. P3-06
      // makes this do the real hard stop.
      label: 'Stop the agent',
      accelerator: KILL_SWITCH_ACCELERATOR,
      enabled: state.taskRunning,
      click: actions.stopAgent,
    },
    { type: 'separator' },
    { label: state.windowVisible ? 'Hide window' : 'Show window', click: actions.toggleWindow },
    { label: 'Quit Aegis', click: actions.quit },
  ];
}
