import { defineConfig } from 'vitest/config';

export default defineConfig({
  // Vite's esbuild plugin does not treat `.cts` as TypeScript by default, and the
  // preload must be CommonJS (REMEMBER.md § 5). Without this the preload's own
  // modules cannot be unit-tested at all.
  esbuild: { include: [/\.[cm]?[jt]sx?$/] },
  test: { include: ['tests/**/*.test.ts'] },
});
