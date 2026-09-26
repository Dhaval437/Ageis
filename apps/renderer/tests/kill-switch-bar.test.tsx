import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BridgeResult, HotkeyMap, KillSwitchStop, StreamEvent } from '@aegis/shared';
import { KillSwitchBar } from '@/components/KillSwitchBar';
import { currentStop, stopDetail } from '@/lib/stop';
import { hudState } from '@/lib/hud-state';
import { INITIAL_KILL_SWITCH, checkKillSwitch, useKillSwitchStore } from '@/stores/kill-switch';
import { INITIAL_STREAM, reduceStream, useStreamStore } from '@/stores/stream';

/** The kill switch in the UI (`UI.md § 7`, P3-14): "Stopped by you", and an unarmed shortcut. */

const AT = '2026-09-26T12:00:00.000Z';
const BEFORE = '2026-09-26T11:59:59.000Z';
const AFTER = '2026-09-26T12:00:01.000Z';

const STOP: KillSwitchStop = { at: AT, outcome: 'acknowledged', elapsedMs: 4 };

let seq = 0;
function status(value: string, ts: string): StreamEvent {
  seq += 1;
  return { seq, ts, task_id: '7', type: 'task.status', payload: { status: value } };
}

beforeEach(() => {
  useStreamStore.setState({ ...INITIAL_STREAM, connection: 'live' });
  useKillSwitchStore.setState({ ...INITIAL_KILL_SWITCH });
});

describe('the stopped message', () => {
  it('is stored, and survives the reset a replaced core sends', () => {
    let state = reduceStream(INITIAL_STREAM, { kind: 'stopped', stop: STOP });
    expect(state.stop).toEqual(STOP);
    state = reduceStream(state, { kind: 'reset' });
    expect(state.stop).toEqual(STOP);
  });

  it.each([
    ['no stop', undefined],
    ['a bad time', { ...STOP, at: 'yesterday' }],
    ['an unknown outcome', { ...STOP, outcome: 'paused' }],
    ['a negative duration', { ...STOP, elapsedMs: -1 }],
    ['a duration that is not a number', { ...STOP, elapsedMs: '4' }],
  ])('rejects %s', (_label, stop) => {
    const state = reduceStream(INITIAL_STREAM, { kind: 'stopped', stop });
    expect(state.stop).toBeNull();
    expect(state.rejected).toBe(1);
  });
});

describe('currentStop', () => {
  it('is the stop while no task has gone again since', () => {
    expect(currentStop({ stop: STOP, events: [status('RUNNING', BEFORE)] })).toEqual(STOP);
    expect(currentStop({ stop: STOP, events: [status('STOPPED', AFTER)] })).toEqual(STOP);
  });

  it.each(['QUEUED', 'RUNNING', 'PAUSED_BY_USER', 'WAITING_APPROVAL'])(
    'gives way to a task that is %s after the stop',
    (value) => {
      expect(currentStop({ stop: STOP, events: [status(value, AFTER)] })).toBeNull();
    },
  );

  it('is not cleared by an event replayed from before the press', () => {
    // The replay after a reset re-delivers old events *after* the stop message.
    expect(currentStop({ stop: STOP, events: [status('RUNNING', AT)] })).toEqual(STOP);
  });

  it('is nothing when there has been no press', () => {
    expect(currentStop({ stop: null, events: [] })).toBeNull();
  });
});

describe('stopDetail', () => {
  it('says what each outcome did, in plain English', () => {
    expect(stopDetail(STOP)).toBe('Aegis stopped in 4 ms and let go of the keyboard and mouse.');
    expect(stopDetail({ ...STOP, outcome: 'terminated', elapsedMs: 101.6 })).toMatch(
      /^The engine didn’t answer, so Aegis ended it after 102 ms/,
    );
    expect(stopDetail({ ...STOP, outcome: 'no-core' })).toBe(
      'Nothing was running, so there was nothing to stop.',
    );
  });
});

describe('the HUD after a press', () => {
  it('says "Stopped by you" even while the replaced engine reconnects', () => {
    const snapshot = { ...INITIAL_STREAM, connection: 'connecting' as const, stop: STOP };
    expect(hudState(snapshot)).toEqual({ kind: 'stopped', failed: false });
  });

  it('follows the task again once it goes again', () => {
    const snapshot = {
      ...INITIAL_STREAM,
      connection: 'live' as const,
      stop: STOP,
      events: [status('RUNNING', AFTER)],
    };
    expect(hudState(snapshot).kind).toBe('running');
  });
});

describe('KillSwitchBar', () => {
  it('is not there when there is nothing to say', () => {
    const { container } = render(<KillSwitchBar />);
    expect(container).toBeEmptyDOMElement();
  });

  it('says "Stopped by you" as an alert, and the person can close it', () => {
    useStreamStore.setState({ stop: STOP });
    render(<KillSwitchBar />);
    expect(screen.getByRole('alert')).toHaveTextContent('Stopped by you.');
    expect(screen.getByRole('alert')).toHaveTextContent('Aegis stopped in 4 ms');
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('shows a newer press after an older one was closed', () => {
    useKillSwitchStore.setState({ dismissedAt: STOP.at });
    useStreamStore.setState({ stop: STOP });
    render(<KillSwitchBar />);
    expect(screen.queryByRole('alert')).toBeNull();
    act(() => {
      useStreamStore.setState({ stop: { ...STOP, at: AFTER } });
    });
    expect(screen.getByRole('alert')).toHaveTextContent('Stopped by you.');
  });

  it('goes once a task starts again', () => {
    useStreamStore.setState({ stop: STOP });
    render(<KillSwitchBar />);
    act(() => {
      useStreamStore.setState({ events: [status('RUNNING', AFTER)] });
    });
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('warns that the shortcut does nothing, and says how to stop meanwhile', () => {
    useKillSwitchStore.setState({
      armed: { kind: 'unarmed', message: 'Another program is using Ctrl+Alt+Shift+Q.' },
    });
    render(<KillSwitchBar />);
    const alert = screen.getByRole('alert');
    expect(alert).toHaveTextContent('The stop shortcut isn’t working.');
    expect(alert).toHaveTextContent('Another program is using Ctrl+Alt+Shift+Q.');
    expect(alert).toHaveTextContent('stop Aegis from its tray icon');
    // Not dismissable: it stays true until the shortcut works.
    expect(screen.queryByRole('button', { name: 'Dismiss' })).toBeNull();
  });
});

describe('checkKillSwitch', () => {
  function bridge(result: BridgeResult<HotkeyMap> | Error) {
    return {
      hotkeys: {
        get: vi.fn(() =>
          result instanceof Error ? Promise.reject(result) : Promise.resolve(result),
        ),
        set: vi.fn(),
      },
    };
  }

  it('records an armed switch', async () => {
    await checkKillSwitch(bridge({ ok: true, value: { killSwitch: 'Control+Alt+Shift+Q' } }));
    expect(useKillSwitchStore.getState().armed).toEqual({ kind: 'armed' });
  });

  it("records MAIN's refusal as unarmed, with its words", async () => {
    await checkKillSwitch(
      bridge({ ok: false, error: { code: 'failed', message: 'Another program has it.' } }),
    );
    expect(useKillSwitchStore.getState().armed).toEqual({
      kind: 'unarmed',
      message: 'Another program has it.',
    });
  });

  it.each([
    ['an unavailable service', { ok: false, error: { code: 'unavailable', message: 'x' } }],
    ['a bridge that throws', new Error('broken')],
  ] as const)('says nothing either way for %s', async (_label, result) => {
    await checkKillSwitch(bridge(result));
    expect(useKillSwitchStore.getState().armed).toEqual({ kind: 'unknown' });
  });

  it('does nothing without a bridge', async () => {
    await checkKillSwitch(undefined);
    expect(useKillSwitchStore.getState().armed).toEqual({ kind: 'unknown' });
  });
});
