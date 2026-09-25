import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { CoreConnection, StreamEvent } from '@aegis/shared';
import { OverlayHUD } from '@/components/OverlayHUD';
import { canStop, hudState, hudText, hudTone } from '@/lib/hud-state';
import { INITIAL_STREAM, useStreamStore, type StreamSnapshot } from '@/stores/stream';

/** The OverlayHUD (`UI.md § 6`, P3-13): what it says, and what it lets a person do. */

type Payload = StreamEvent['payload'];

let seq = 0;
function event(type: StreamEvent['type'], payload: Payload): StreamEvent {
  seq += 1;
  return { seq, ts: new Date().toISOString(), task_id: '7', type, payload };
}

const APPROVAL: Payload = {
  approval_id: 5,
  tool: 'fs.delete',
  prompt: 'Delete 12 files',
  why: null,
  tier: 'DANGEROUS',
  reason: 'Asks first.',
  timeout_s: 30,
  allow_always: true,
  rule_kinds: ['exact'],
  folder: null,
  reversible: false,
};

function snapshot(connection: CoreConnection | null, events: StreamEvent[] = []): StreamSnapshot {
  return {
    ...INITIAL_STREAM,
    connection,
    hasBeenLive: connection === 'live',
    events,
    lastSeq: events.at(-1)?.seq ?? 0,
  };
}

const hud = {
  subscribe: vi.fn(() => () => undefined),
  stop: vi.fn(),
  showMain: vi.fn(),
  deny: vi.fn(() => Promise.resolve({ ok: true as const, value: null })),
};

beforeEach(() => {
  vi.clearAllMocks();
  useStreamStore.setState(INITIAL_STREAM);
  vi.stubGlobal('aegisHud', hud);
});

function show(state: StreamSnapshot): void {
  act(() => {
    useStreamStore.setState(state);
  });
}

describe('hudState', () => {
  it('says it is connecting before the stream has spoken, and offline once it is gone', () => {
    expect(hudState(snapshot(null)).kind).toBe('connecting');
    expect(hudState(snapshot('unavailable')).kind).toBe('offline');
    expect(hudState({ ...snapshot('connecting'), hasBeenLive: true }).kind).toBe('offline');
  });

  it('puts a pending approval above the task that is waiting on it', () => {
    const state = hudState(
      snapshot('live', [
        event('task.status', { status: 'RUNNING' }),
        event('approval.requested', APPROVAL),
      ]),
    );
    expect(state.kind).toBe('approval');
    expect(hudTone(state)).toBe('attention');
    expect(hudText(state)).toBe('Waiting for you: Delete 12 files');
  });

  it.each([
    ['RUNNING', 'running', 'active', 'Working…'],
    ['PAUSED_BY_USER', 'paused', 'attention', 'You took over — Aegis paused.'],
    ['STOPPED', 'stopped', 'danger', 'Stopped by you.'],
    ['FAILED', 'stopped', 'danger', 'Stopped. The task did not finish.'],
    ['DONE', 'idle', 'neutral', 'Aegis is ready.'],
  ])('reads task status %s', (status, kind, tone, text) => {
    const state = hudState(snapshot('live', [event('task.status', { status })]));
    expect([state.kind, hudTone(state), hudText(state)]).toEqual([kind, tone, text]);
  });

  it('shows the current action while running, and forgets it when the task changes', () => {
    const running = snapshot('live', [
      event('task.status', { status: 'RUNNING' }),
      event('step.action', { summary: 'Clicking "Date modified"…' }),
    ]);
    expect(hudText(hudState(running))).toBe('Clicking "Date modified"…');
    const next = snapshot('live', [
      event('step.action', { summary: 'old' }),
      event('task.status', { status: 'RUNNING' }),
    ]);
    expect(hudText(hudState(next))).toBe('Working…');
  });

  it('offers Stop only when there is something to stop', () => {
    expect(canStop(hudState(snapshot('live')))).toBe(false);
    expect(canStop(hudState(snapshot('unavailable')))).toBe(false);
    expect(canStop(hudState(snapshot('live', [event('task.status', { status: 'RUNNING' })])))).toBe(
      true,
    );
  });
});

describe('OverlayHUD', () => {
  it('announces its status assertively', () => {
    render(<OverlayHUD />);
    show(snapshot('live'));
    expect(screen.getByRole('status')).toHaveAttribute('aria-live', 'assertive');
    expect(screen.getByText('Aegis is ready.')).toBeVisible();
  });

  it('offers Deny and Review for an approval, and never Allow', async () => {
    render(<OverlayHUD />);
    show(snapshot('live', [event('approval.requested', APPROVAL)]));
    expect(screen.queryByRole('button', { name: /allow/i })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Deny' }));
    await act(async () => {
      await Promise.resolve();
    });
    expect(hud.deny).toHaveBeenCalledExactlyOnceWith(5);
    expect(screen.getByRole('button', { name: 'Denying…' })).toBeDisabled();
    fireEvent.click(screen.getByRole('button', { name: 'Review' }));
    expect(hud.showMain).toHaveBeenCalledOnce();
  });

  it('stops through the kill switch', () => {
    render(<OverlayHUD />);
    show(snapshot('live', [event('task.status', { status: 'RUNNING' })]));
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(hud.stop).toHaveBeenCalledOnce();
  });

  it('opens the main window from the chevron and on double-click', () => {
    render(<OverlayHUD />);
    show(snapshot('live'));
    fireEvent.click(screen.getByRole('button', { name: 'Open Aegis' }));
    fireEvent.doubleClick(screen.getByRole('status'));
    expect(hud.showMain).toHaveBeenCalledTimes(2);
  });

  it('keeps its buttons out of the window-drag region', () => {
    render(<OverlayHUD />);
    show(snapshot('live', [event('task.status', { status: 'RUNNING' })]));
    const stop = screen.getByRole('button', { name: 'Stop' });
    expect(stop.closest('.app-no-drag')).not.toBeNull();
    expect(screen.getByRole('status')).toHaveClass('app-drag');
  });

  it('does nothing, and does not throw, without its bridge', () => {
    vi.stubGlobal('aegisHud', undefined);
    render(<OverlayHUD />);
    show(snapshot('live', [event('task.status', { status: 'RUNNING' })]));
    expect(() => {
      fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    }).not.toThrow();
  });
});
