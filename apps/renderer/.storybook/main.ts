import type { StorybookConfig } from '@storybook/react-vite';

/**
 * Storybook for the renderer (P0-13). It reuses `vite.config.ts`, so stories get
 * the same Tailwind build, the same cleared token scales and the same `@/` alias
 * as the app.
 *
 * Dev tooling only: nothing here is packaged. It still sends nothing anywhere
 * (`REMEMBER.md` invariant 10 is the habit), so telemetry and the "what's new"
 * fetch are off, and the scripts pass `--no-version-updates`.
 */
const config: StorybookConfig = {
  framework: '@storybook/react-vite',
  stories: ['../src/**/*.stories.tsx'],
  core: {
    disableTelemetry: true,
    disableWhatsNewNotifications: true,
  },
};

export default config;
