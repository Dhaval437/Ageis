import type { Decorator, Preview } from '@storybook/react-vite';
import { INITIAL_STREAM, useStreamStore } from '../src/stores/stream';
import { installStoryBridge } from './story-bridge';
import '../src/index.css';

installStoryBridge();

/**
 * Both palettes are explicit blocks in `index.css`, selected by `data-theme`, so
 * the toolbar sets exactly what the Settings toggle will (`UI.md § 8.6`).
 * `REVIEW.md § 3` asks for contrast in both themes; this is where to look.
 */
const withTheme: Decorator = (Story, context) => {
  const theme = context.globals['theme'] === 'light' ? 'light' : 'dark';
  document.documentElement.dataset['theme'] = theme;
  return <Story />;
};

const preview: Preview = {
  globalTypes: {
    theme: {
      description: 'Theme',
      toolbar: {
        title: 'Theme',
        icon: 'mirror',
        items: [
          { value: 'dark', title: 'Dark' },
          { value: 'light', title: 'Light' },
        ],
        dynamicTitle: true,
      },
    },
  },
  initialGlobals: { theme: 'dark' },
  decorators: [withTheme],
  // Every story starts from an empty stream; a story that needs state sets it.
  beforeEach: () => {
    useStreamStore.setState(INITIAL_STREAM);
  },
  parameters: {
    layout: 'fullscreen',
  },
};

export default preview;
