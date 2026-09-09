import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { AppShell } from '@/components/AppShell';

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
});
