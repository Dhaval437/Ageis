import { describe, expect, it, vi } from 'vitest';
import type { MenuItem } from 'electron';
import {
  KILL_SWITCH_ACCELERATOR,
  buildTrayMenuTemplate,
  type TrayMenuActions,
  type TrayMenuState,
} from '../src/main/tray-menu.js';

function actions(): TrayMenuActions {
  return { toggleWindow: vi.fn(), stopAgent: vi.fn(), quit: vi.fn() };
}

function labelled(state: TrayMenuState, label: string) {
  return buildTrayMenuTemplate(state, actions()).find((item) => item.label === label);
}

const IDLE: TrayMenuState = { windowVisible: true, taskRunning: false };
const RUNNING: TrayMenuState = { windowVisible: true, taskRunning: true };

describe('buildTrayMenuTemplate', () => {
  it('always offers a way to show the window and to quit (invariant 14)', () => {
    for (const state of [IDLE, RUNNING]) {
      const labels = buildTrayMenuTemplate(state, actions()).map((item) => item.label);
      expect(labels).toContain('Quit Aegis');
      expect(labels.some((label) => label === 'Show window' || label === 'Hide window')).toBe(true);
    }
  });

  it('toggles the window label to match visibility', () => {
    expect(labelled({ ...IDLE, windowVisible: true }, 'Hide window')).toBeDefined();
    expect(labelled({ ...IDLE, windowVisible: false }, 'Show window')).toBeDefined();
  });

  it('carries the kill-switch accelerator on the stop item', () => {
    expect(labelled(RUNNING, 'Stop the agent')?.accelerator).toBe(KILL_SWITCH_ACCELERATOR);
  });

  it('enables stop only while a task is running', () => {
    expect(labelled(RUNNING, 'Stop the agent')?.enabled).toBe(true);
    expect(labelled(IDLE, 'Stop the agent')?.enabled).toBe(false);
  });

  it('reports task state in the status item, which is never clickable', () => {
    const status = buildTrayMenuTemplate(RUNNING, actions())[0];
    expect(status?.label).toBe('Aegis — running');
    expect(status?.enabled).toBe(false);
    expect(buildTrayMenuTemplate(IDLE, actions())[0]?.label).toBe('Aegis — idle');
  });

  it('wires each action to its own item', () => {
    const spies = actions();
    const template = buildTrayMenuTemplate(RUNNING, spies);
    // Our handlers ignore every argument; stubs keep the signature honest.
    const click = (label: string) => {
      const item = template.find((entry) => entry.label === label);
      item?.click?.({} as MenuItem, undefined, {});
    };

    click('Hide window');
    click('Stop the agent');
    click('Quit Aegis');

    expect(spies.toggleWindow).toHaveBeenCalledOnce();
    expect(spies.stopAgent).toHaveBeenCalledOnce();
    expect(spies.quit).toHaveBeenCalledOnce();
  });
});
