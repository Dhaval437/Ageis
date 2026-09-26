/**
 * The tray menu template (`UI.md § 3`: status, kill switch, show/hide, quit).
 *
 * Pure and `electron`-free at runtime — the only import is a type, which is
 * erased — so the menu's shape is unit-testable without an Electron process.
 */

import type { MenuItemConstructorOptions } from 'electron';

export interface TrayMenuState {
  readonly windowVisible: boolean;
  readonly taskRunning: boolean;
  /** The kill-switch binding in force (`hotkeys.ts`), shown beside *Stop the agent*. */
  readonly killSwitch: string;
  /**
   * Whether Windows gave Aegis that shortcut (P3-14). When another program owns it,
   * the menu says so rather than showing a shortcut that does nothing.
   */
  readonly killSwitchArmed: boolean;
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
      // Disabled while nothing is running: there is no agent to stop. When
      // enabled it is the kill switch itself (`kill-switch.ts`), for the user
      // who reaches for the mouse instead of the keyboard.
      label: state.killSwitchArmed ? 'Stop the agent' : 'Stop the agent (shortcut not working)',
      ...(state.killSwitchArmed ? { accelerator: state.killSwitch } : {}),
      // Display only: the global shortcut is registered by `hotkeys.ts`, and a
      // second registration here would be one the rebind could not move.
      registerAccelerator: false,
      enabled: state.taskRunning,
      click: actions.stopAgent,
    },
    { type: 'separator' },
    { label: state.windowVisible ? 'Hide window' : 'Show window', click: actions.toggleWindow },
    { label: 'Quit Aegis', click: actions.quit },
  ];
}
