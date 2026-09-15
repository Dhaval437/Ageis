import type { Meta, StoryObj } from '@storybook/react-vite';
import type { AegisStoryParameters } from '../../../.storybook/states';
import { Button } from './button';

const aegis: AegisStoryParameters = {
  notApplicable: {
    Loading:
      'Button has no loading state. The first component that needs a pending button adds the prop and this story with it.',
    Empty: 'A button always has a label or, for `size="icon"`, an aria-label.',
    Error:
      'A button cannot fail; the action it starts reports its own error where the user is looking (`UI.md § 9`).',
  },
};

const meta = {
  title: 'Primitives/Button',
  component: Button,
  args: { children: 'Start' },
  parameters: { layout: 'padded', aegis },
} satisfies Meta<typeof Button>;

export default meta;

type Story = StoryObj<typeof meta>;

export const Default: Story = {};

/** Too long for one line; the button refuses to wrap, so its container has to cope. */
export const LongestContent: Story = {
  args: { children: 'Restore 12 files to C:\\Users\\you\\Documents\\Invoices\\2026\\September' },
};

/** `danger` is for irreversible actions only (`UI.md § 2`). */
export const Variants: Story = {
  render: (args) => (
    <div className="flex flex-wrap gap-2">
      <Button {...args} variant="accent" />
      <Button {...args} variant="secondary" />
      <Button {...args} variant="ghost" />
      <Button {...args} variant="danger">
        Delete
      </Button>
    </div>
  ),
};

export const Disabled: Story = {
  args: { variant: 'accent', disabled: true },
};
