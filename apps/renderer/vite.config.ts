import { fileURLToPath, URL } from 'node:url';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  // Electron loads the built renderer from disk via a file:// URL.
  base: './',
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  build: {
    outDir: 'dist',
    emptyOutDir: true,
    // Two pages: the main window, and the OverlayHUD window (P3-13).
    rollupOptions: {
      input: {
        main: fileURLToPath(new URL('./index.html', import.meta.url)),
        hud: fileURLToPath(new URL('./hud.html', import.meta.url)),
      },
    },
    sourcemap: true,
    // A small font subset would otherwise be inlined as a `data:` URI, which the
    // CSP's `default-src 'self'` refuses. Fonts are always emitted as files.
    assetsInlineLimit: (file) => (file.endsWith('.woff2') ? false : undefined),
  },
  server: {
    port: 5173,
    strictPort: true,
  },
});
