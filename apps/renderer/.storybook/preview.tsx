import type { Decorator, Preview } from '@storybook/react-vite';
import { INITIAL_MODELS, useModelsStore } from '../src/stores/models';
import { INITIAL_RECOVERY, useRecoveryStore } from '../src/stores/recovery';
import { INITIAL_STREAM, useStreamStore } from '../src/stores/stream';
import { INITIAL_WINDOW, useWindowStore } from '../src/stores/window';
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
  // Every story starts from an empty stream, a restored window, an idle recovery
  // screen and an unloaded Models screen; a story that needs state sets it.
  beforeEach: () => {
    useStreamStore.setState(INITIAL_STREAM);
    useWindowStore.setState(INITIAL_WINDOW);
    useRecoveryStore.setState(INITIAL_RECOVERY);
    useModelsStore.setState(INITIAL_MODELS);
  },
  parameters: {
    layout: 'fullscreen',
  },
};

export default preview;
