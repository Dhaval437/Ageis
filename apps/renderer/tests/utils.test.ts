import { describe, expect, it } from 'vitest';
import { cn } from '@/lib/utils';

describe('cn', () => {
  it('drops falsy values', () => {
    expect(cn('a', false, undefined, 'b')).toBe('a b');
  });

  it('lets the caller override a component default', () => {
    expect(cn('bg-surface-1', 'bg-surface-2')).toBe('bg-surface-2');
  });

  it('treats an AEGIS font size and an AEGIS text colour as different properties', () => {
    expect(cn('text-md', 'text-danger')).toBe('text-md text-danger');
  });

  it('collapses two AEGIS font sizes', () => {
    expect(cn('text-md', 'text-xs')).toBe('text-xs');
  });

  it('collapses two AEGIS radii', () => {
    expect(cn('rounded-card', 'rounded-pill')).toBe('rounded-pill');
  });
});
