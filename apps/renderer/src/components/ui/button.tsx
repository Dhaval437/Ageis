import type { ComponentProps, ReactElement } from 'react';
import { Slot } from '@radix-ui/react-slot';
import { cva, type VariantProps } from 'class-variance-authority';
import { cn } from '@/lib/utils';

/**
 * The base control, shadcn-style, restricted to the AEGIS tokens.
 *
 * `danger` is for irreversible actions only (`UI.md § 2`): a `--danger` control
 * always means "this cannot be taken back", never just "destructive-looking".
 */
const buttonVariants = cva(
  'inline-flex shrink-0 items-center justify-center gap-2 rounded-control border font-medium whitespace-nowrap transition-colors duration-120 ease-out disabled:pointer-events-none disabled:opacity-50 [&_svg]:size-4 [&_svg]:shrink-0',
  {
    variants: {
      variant: {
        accent: 'border-accent bg-accent text-bg hover:opacity-90',
        secondary: 'border-border bg-surface-2 text-text hover:border-text-dim',
        ghost: 'border-transparent bg-transparent text-text-dim hover:bg-surface-2 hover:text-text',
        danger: 'border-danger bg-danger text-bg hover:opacity-90',
      },
      size: {
        sm: 'h-7 px-2.5 text-sm',
        md: 'h-8 px-3 text-base',
        icon: 'size-8 p-0',
      },
    },
    defaultVariants: {
      variant: 'secondary',
      size: 'md',
    },
  },
);

export type ButtonProps = ComponentProps<'button'> &
  VariantProps<typeof buttonVariants> & {
    /** Render the child element instead of a `<button>` (Radix `Slot`). */
    asChild?: boolean;
  };

export function Button({
  className,
  variant,
  size,
  asChild = false,
  type = 'button',
  ...props
}: ButtonProps): ReactElement {
  const Component = asChild ? Slot : 'button';
  return (
    <Component
      data-slot="button"
      className={cn(buttonVariants({ variant, size }), className)}
      {...(asChild ? {} : { type })}
      {...props}
    />
  );
}
