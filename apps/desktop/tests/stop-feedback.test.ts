import { describe, expect, it, vi } from 'vitest';
import type { CoreStreamMessage } from '@aegis/shared';
import { createStopFeedback } from '../src/main/stop-feedback.js';
import type { KillReport } from '../src/main/kill-switch.js';

const AT = new Date('2026-09-26T12:00:00.000Z');

function setup() {
  const delivered: CoreStreamMessage[] = [];
  const order: string[] = [];
  const options = {
    deliver: vi.fn((message: CoreStreamMessage) => {
      delivered.push(message);
      order.push(`deliver:${message.kind}`);
    }),
    flash: vi.fn(() => {
      order.push('flash');
    }),
    showMain: vi.fn(() => {
      order.push('showMain');
    }),
    onStopped: vi.fn(() => {
      order.push('hud');
    }),
    now: () => AT,
  };
  return { feedback: createStopFeedback(options), options, delivered, order };
}

const REPORT: KillReport = { outcome: 'terminated', elapsedMs: 102.4, releaseFailures: 0 };

describe('createStopFeedback', () => {
  it('tells every page, shows the HUD, flashes and brings the main window forward', () => {
    const { feedback, delivered, order } = setup();
    feedback.reported(REPORT);
    expect(delivered).toEqual([
      {
        kind: 'stopped',
        stop: { at: '2026-09-26T12:00:00.000Z', outcome: 'terminated', elapsedMs: 102 },
      },
    ]);
    // The pages learn first, so the window that comes forward already says it.
    expect(order).toEqual(['deliver:stopped', 'hud', 'flash', 'showMain']);
  });

  it('does nothing before the stop has been reported', () => {
    const { feedback, options } = setup();
    feedback.observe({ kind: 'reset' });
    expect(options.deliver).not.toHaveBeenCalled();
    expect(options.flash).not.toHaveBeenCalled();
    expect(feedback.last()).toBeNull();
  });

  it('sends the stop again after every reset, because a new core knows nothing of it', () => {
    const { feedback, delivered } = setup();
    feedback.reported(REPORT);
    feedback.observe({ kind: 'reset' });
    feedback.observe({ kind: 'reset' });
    expect(delivered.map((message) => message.kind)).toEqual(['stopped', 'stopped', 'stopped']);
    expect(delivered[1]).toEqual(delivered[0]);
  });

  it('ignores everything but a reset', () => {
    const { feedback, options } = setup();
    feedback.reported(REPORT);
    options.deliver.mockClear();
    feedback.observe({ kind: 'connection', state: 'live' });
    feedback.observe({ kind: 'event', event: {} });
    expect(options.deliver).not.toHaveBeenCalled();
  });

  it('keeps the latest press', () => {
    const { feedback } = setup();
    feedback.reported(REPORT);
    feedback.reported({ outcome: 'acknowledged', elapsedMs: 3.6, releaseFailures: 0 });
    expect(feedback.last()).toEqual({
      at: '2026-09-26T12:00:00.000Z',
      outcome: 'acknowledged',
      elapsedMs: 4,
    });
  });

  it('a flash that throws still brings the main window forward', () => {
    const { feedback, options } = setup();
    options.flash.mockImplementation(() => {
      throw new Error('no display');
    });
    const error = vi.spyOn(console, 'error').mockImplementation(() => undefined);
    feedback.reported(REPORT);
    expect(options.showMain).toHaveBeenCalledOnce();
    expect(error).toHaveBeenCalled();
    error.mockRestore();
  });
});
