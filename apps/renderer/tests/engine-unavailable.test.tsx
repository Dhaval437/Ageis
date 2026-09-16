import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BridgeResult } from '@aegis/shared';
import { AppShell } from '@/components/AppShell';
import { EngineUnavailable } from '@/components/EngineUnavailable';
import { recoveryNotice } from '@/lib/engine-recovery';
import { INITIAL_RECOVERY, useRecoveryStore } from '@/stores/recovery';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';
import { INITIAL_WINDOW, useWindowStore } from '@/stores/window';

/**
 * `RECOVERY.md § 4`'s Engine-unavailable screen (P0-17): the three actions, what
 * each of them says when it fails, and the fact that the screen appears at all
 * only because the supervisor gave up.
 */

const ok: BridgeResult<null> = { ok: true, value: null };
const failed: BridgeResult<null> = { ok: false, error: { code: 'failed', message: 'nope' } };
const unavailable: BridgeResult<null> = {
  ok: false,
  error: { code: 'unavailable', message: 'not running' },
};

/** Typed on the contract, not narrowed to the first value handed to them. */
const result = (value: BridgeResult<null>): Promise<BridgeResult<null>> => Promise.resolve(value);

const core = {
  restart: vi.fn((): Promise<BridgeResult<null>> => result(ok)),
  subscribe: vi.fn(),
};
const app = {
  logsPath: vi.fn((): Promise<string> => Promise.resolve('C:\\Logs')),
  copyDiagnosticReport: vi.fn((): Promise<BridgeResult<null>> => result(ok)),
};
const system = { openPath: vi.fn((): Promise<BridgeResult<null>> => result(ok)) };
const windowBridge = { minimize: vi.fn(), maximize: vi.fn(), close: vi.fn() };

beforeEach(() => {
  vi.clearAllMocks();
  core.restart.mockResolvedValue(ok);
  app.logsPath.mockResolvedValue('C:\\Logs');
  app.copyDiagnosticReport.mockResolvedValue(ok);
  system.openPath.mockResolvedValue(ok);
  useRecoveryStore.setState(INITIAL_RECOVERY);
  useStreamStore.setState(INITIAL_STREAM);
  useWindowStore.setState(INITIAL_WINDOW);
  vi.stubGlobal('aegis', { core, app, system, window: windowBridge });
});

describe('EngineUnavailable', () => {
  it('says what happened and offers the three actions from RECOVERY.md § 4', () => {
    render(<EngineUnavailable />);
    expect(screen.getByRole('heading', { name: 'The Aegis engine isn’t running' })).toBeVisible();
    expect(
      screen.getByText(
        'It stopped 3 times in a row. Your data is safe and your task history is intact.',
      ),
    ).toBeVisible();
    for (const name of ['Restart engine', 'Open logs', 'Copy report']) {
      expect(screen.getByRole('button', { name })).toBeEnabled();
    }
    expect(screen.getByText(/antivirus blocked aegis-core\.exe/)).toBeVisible();
  });

  it('restarts the engine through the bridge, with no argument', async () => {
    render(<EngineUnavailable />);
    fireEvent.click(screen.getByRole('button', { name: 'Restart engine' }));
    expect(core.restart).toHaveBeenCalledExactlyOnceWith();
    // A restart that worked takes the screen away; it does not congratulate itself.
    await waitFor(() => {
      expect(useRecoveryStore.getState()).toMatchObject(INITIAL_RECOVERY);
    });
  });

  it('opens the log folder MAIN names, never a path of its own', async () => {
    render(<EngineUnavailable />);
    fireEvent.click(screen.getByRole('button', { name: 'Open logs' }));
    await waitFor(() => {
      expect(system.openPath).toHaveBeenCalledExactlyOnceWith('C:\\Logs');
    });
  });

  it('reports a log folder MAIN would not name, rather than opening nothing', async () => {
    app.logsPath.mockResolvedValue('');
    render(<EngineUnavailable />);
    fireEvent.click(screen.getByRole('button', { name: 'Open logs' }));
    expect(await screen.findByText(/could not open the log folder/i)).toBeVisible();
    expect(system.openPath).not.toHaveBeenCalled();
  });

  it('copies the report in MAIN and confirms it, because the clipboard shows nothing', async () => {
    render(<EngineUnavailable />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy report' }));
    expect(app.copyDiagnosticReport).toHaveBeenCalledExactlyOnceWith();
    expect(await screen.findByText(/Report copied\./)).toBeVisible();
  });

  it('says what to try next when the restart fails', async () => {
    core.restart.mockResolvedValue(failed);
    render(<EngineUnavailable />);
    fireEvent.click(screen.getByRole('button', { name: 'Restart engine' }));
    expect(await screen.findByText(/The engine did not start\./)).toBeVisible();
  });

  it('tells the user to relaunch when MAIN itself cannot start the engine', async () => {
    core.restart.mockResolvedValue(unavailable);
    render(<EngineUnavailable />);
    fireEvent.click(screen.getByRole('button', { name: 'Restart engine' }));
    expect(await screen.findByText(/Close Aegis and open it again\./)).toBeVisible();
  });

  it('disables every action while one is in flight, and re-enables them after', async () => {
    let settle = (_result: BridgeResult<null>): void => undefined;
    core.restart.mockReturnValue(
      new Promise<BridgeResult<null>>((resolve) => {
        settle = resolve;
      }),
    );
    render(<EngineUnavailable />);
    fireEvent.click(screen.getByRole('button', { name: 'Restart engine' }));

    expect(screen.getByRole('button', { name: /Starting…/ })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Open logs/ })).toBeDisabled();

    settle(failed);
    await waitFor(() => {
      expect(screen.getByRole('button', { name: /Restart engine/ })).toBeEnabled();
    });
  });

  it('announces its outcome politely, not assertively', async () => {
    render(<EngineUnavailable />);
    fireEvent.click(screen.getByRole('button', { name: 'Copy report' }));
    const live = await screen.findByText(/Report copied\./);
    expect(live.closest('[aria-live]')).toHaveAttribute('aria-live', 'polite');
  });
});

describe('AppShell', () => {
  it('shows the recovery screen only once the supervisor has given up', () => {
    const { rerender } = render(<AppShell />);
    expect(screen.queryByRole('heading', { name: /engine isn’t running/ })).not.toBeInTheDocument();

    useStreamStore.getState().apply({ kind: 'connection', state: 'down' });
    rerender(<AppShell />);
    expect(screen.queryByRole('heading', { name: /engine isn’t running/ })).not.toBeInTheDocument();
    expect(screen.getByText('No task is running.')).toBeInTheDocument();

    useStreamStore.getState().apply({ kind: 'connection', state: 'unavailable' });
    rerender(<AppShell />);
    expect(screen.getByRole('heading', { name: /engine isn’t running/ })).toBeVisible();
    expect(screen.queryByText('No task is running.')).not.toBeInTheDocument();
  });
});

describe('recoveryNotice', () => {
  it('says nothing about the actions that show their own result', () => {
    expect(recoveryNotice('restart', ok)).toBeNull();
    expect(recoveryNotice('logs', ok)).toBeNull();
  });

  it('reports every failure with something to do next', () => {
    for (const action of ['restart', 'logs', 'report'] as const) {
      const notice = recoveryNotice(action, failed);
      expect(notice?.tone).toBe('error');
      expect(notice?.text.length ?? 0).toBeGreaterThan(0);
    }
  });

  it('never quotes the bridge error, which is written for a developer', () => {
    expect(recoveryNotice('restart', failed)?.text).not.toContain('nope');
  });
});
