import { describe, expect, it, vi } from 'vitest';
import { STOP_LINGER_MS, createHudVisibility } from '../src/main/hud-visibility.js';

interface FakeTimer {
  readonly callback: () => void;
  readonly ms: number;
  cleared: boolean;
}

function setup() {
  let taskActive = false;
  const timers: FakeTimer[] = [];
  const setVisible = vi.fn();
  const visibility = createHudVisibility({
    setVisible,
    taskActive: () => taskActive,
    setTimer: (callback, ms) => {
      const timer: FakeTimer = { callback, ms, cleared: false };
      timers.push(timer);
      return timer;
    },
    clearTimer: (handle) => {
      (handle as FakeTimer).cleared = true;
    },
  });
  return {
    visibility,
    setVisible,
    timers,
    setActive: (value: boolean) => {
      taskActive = value;
      visibility.observe();
    },
    fire: () => {
      const timer = timers.at(-1);
      if (timer !== undefined && !timer.cleared) timer.callback();
    },
  };
}

describe('createHudVisibility', () => {
  it('shows the HUD when a task becomes active, and hides it when none is', () => {
    const { setVisible, setActive } = setup();
    setActive(true);
    expect(setVisible).toHaveBeenLastCalledWith(true);
    setActive(false);
    expect(setVisible).toHaveBeenLastCalledWith(false);
    expect(setVisible).toHaveBeenCalledTimes(2);
  });

  it('acts on changes only, so a hand-set HUD is not undone by every event', () => {
    const { visibility, setVisible, setActive } = setup();
    visibility.observe();
    visibility.observe();
    expect(setVisible).not.toHaveBeenCalled();
    setActive(true);
    visibility.observe();
    visibility.observe();
    expect(setVisible).toHaveBeenCalledTimes(1);
  });

  it('shows "Stopped by you" for the linger, then hides it with no task active', () => {
    const { visibility, setVisible, timers, fire } = setup();
    visibility.stopped();
    expect(setVisible).toHaveBeenLastCalledWith(true);
    expect(timers[0]?.ms).toBe(STOP_LINGER_MS);
    fire();
    expect(setVisible).toHaveBeenLastCalledWith(false);
  });

  it('a task ending because of the stop does not take the HUD down early', () => {
    const { visibility, setVisible, setActive, fire } = setup();
    setActive(true);
    visibility.stopped();
    setActive(false); // the core reports STOPPED
    expect(setVisible).toHaveBeenLastCalledWith(true);
    fire();
    expect(setVisible).toHaveBeenLastCalledWith(false);
  });

  it('keeps the HUD up after the linger while a task is still active', () => {
    const { visibility, setVisible, setActive, fire } = setup();
    setActive(true);
    visibility.stopped();
    fire();
    expect(setVisible).toHaveBeenLastCalledWith(true);
  });

  it('a task starting during the linger cancels it', () => {
    const { visibility, timers, setActive } = setup();
    visibility.stopped();
    setActive(true);
    expect(timers[0]?.cleared).toBe(true);
  });

  it('a second press restarts the linger', () => {
    const { visibility, timers } = setup();
    visibility.stopped();
    visibility.stopped();
    expect(timers).toHaveLength(2);
    expect(timers[0]?.cleared).toBe(true);
  });

  it('dispose cancels a pending linger', () => {
    const { visibility, timers } = setup();
    visibility.stopped();
    visibility.dispose();
    expect(timers[0]?.cleared).toBe(true);
  });
});
