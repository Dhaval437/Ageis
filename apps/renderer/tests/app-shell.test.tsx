import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AppShell } from '@/components/AppShell';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';
import { INITIAL_WINDOW, useWindowStore } from '@/stores/window';

/**
 * The preload injects `window.aegis` before any renderer code runs. jsdom has
 * no preload, so the two methods the shell actually calls are stubbed here.
 */
const windowBridge = { minimize: vi.fn(), maximize: vi.fn(), close: vi.fn() };

beforeEach(() => {
  vi.clearAllMocks();
  useStreamStore.setState(INITIAL_STREAM);
  useWindowStore.setState(INITIAL_WINDOW);
  vi.stubGlobal('aegis', { window: windowBridge });
});

describe('AppShell', () => {
  it('renders the six rail destinations from UI.md § 4', () => {
    render(<AppShell />);
    const rail = screen.getByRole('navigation', { name: 'Sections' });
    const labels = Array.from(rail.querySelectorAll('button')).map((button) => button.textContent);
    expect(labels).toEqual(['Tasks', 'History', 'Rules', 'Models', 'Logs', 'Settings']);
  });

  it('starts on Tasks and marks it current', () => {
    render(<AppShell />);
    expect(screen.getByRole('button', { name: 'Tasks' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByText('No task is running.')).toBeInTheDocument();
  });

  it('swaps the empty state when another section is selected', () => {
    render(<AppShell />);
    fireEvent.click(screen.getByRole('button', { name: 'History' }));
    expect(screen.getByRole('button', { name: 'History' })).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('button', { name: 'Tasks' })).not.toHaveAttribute('aria-current');
    expect(screen.getByText('No tasks yet.')).toBeInTheDocument();
  });

  it('disables the composer while no model is connected (UI.md § 9)', () => {
    render(<AppShell />);
    expect(screen.getByLabelText('Ask Aegis to do something on your PC')).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Start' })).toBeDisabled();
    expect(screen.getByText('Connect a model to get started.')).toBeInTheDocument();
  });

  it('gives the frameless window a drag region that excludes its controls', () => {
    const { container } = render(<AppShell />);
    const titlebar = container.querySelector('header');
    expect(titlebar).toHaveClass('app-drag');
    expect(titlebar?.querySelectorAll('.app-no-drag').length).toBeGreaterThan(0);
  });

  it('drives the window controls through the preload bridge', () => {
    render(<AppShell />);

    fireEvent.click(screen.getByRole('button', { name: 'Minimise' }));
    expect(windowBridge.minimize).toHaveBeenCalledOnce();
    expect(windowBridge.close).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Maximise' }));
    expect(windowBridge.maximize).toHaveBeenCalledOnce();

    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(windowBridge.close).toHaveBeenCalledOnce();
  });

  it('offers Restore instead of Maximise once MAIN says the window is maximised', () => {
    render(<AppShell />);
    expect(screen.queryByRole('button', { name: 'Restore' })).not.toBeInTheDocument();

    act(() => {
      useWindowStore.getState().setMaximized(true);
    });
    expect(screen.queryByRole('button', { name: 'Maximise' })).not.toBeInTheDocument();

    // Restoring goes through the same one toggle: MAIN owns which way it flips.
    fireEvent.click(screen.getByRole('button', { name: 'Restore' }));
    expect(windowBridge.maximize).toHaveBeenCalledOnce();

    act(() => {
      useWindowStore.getState().setMaximized(false);
    });
    expect(screen.getByRole('button', { name: 'Maximise' })).toBeInTheDocument();
  });

  it('keeps the window controls out of the drag region, or they cannot be clicked', () => {
    render(<AppShell />);
    for (const name of ['Minimise', 'Maximise', 'Close']) {
      expect(screen.getByRole('button', { name }).closest('.app-no-drag'), name).not.toBeNull();
    }
  });

  it('shows the engine starting until the stream is live, then idle', () => {
    render(<AppShell />);
    expect(screen.getByRole('status')).toHaveTextContent('Starting the engine…');

    act(() => {
      useStreamStore.getState().apply({ kind: 'connection', state: 'live' });
    });
    expect(screen.queryByRole('status')).not.toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Status: idle' })).toBeInTheDocument();

    act(() => {
      useStreamStore.getState().apply({ kind: 'connection', state: 'down' });
    });
    expect(screen.getByRole('status')).toHaveTextContent('Reconnecting…');
  });

  it('marks the status red when the engine is unavailable', () => {
    useStreamStore.getState().apply({ kind: 'connection', state: 'unavailable' });
    render(<AppShell />);
    expect(screen.getByRole('img', { name: 'Status: the engine is not running' })).toHaveClass(
      'bg-danger',
    );
  });
});
