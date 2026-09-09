import { clsx, type ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

/**
 * `index.css` clears Tailwind's stock colour/size/radius scales and declares the
 * AEGIS ones from `UI.md § 2`, so tailwind-merge has to be told the same scales.
 * Without this it cannot tell `text-md` (a size) from `text-danger` (a colour)
 * and silently keeps both when one should override the other.
 */
const twMerge = extendTailwindMerge({
  extend: {
    theme: {
      color: [
        'bg',
        'surface-1',
        'surface-2',
        'border',
        'text',
        'text-dim',
        'accent',
        'safe',
        'caution',
        'danger',
        'forbidden',
      ],
      text: ['xs', 'sm', 'base', 'md', 'lg', 'xl'],
      radius: ['card', 'control', 'pill'],
    },
  },
});

/**
 * The shadcn/ui class helper: conditional classes via `clsx`, then conflict
 * resolution via `tailwind-merge` so a caller's `className` always wins over a
 * component's own default.
 */
export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
