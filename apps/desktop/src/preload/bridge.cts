/**
 * The preload bridge — the ONLY surface exposed to the renderer.
 *
 * The renderer is sandboxed with `nodeIntegration: false` and has zero fs/net
 * access (ARCHITECTURE.md § 3). Everything it can do is enumerated here via
 * `contextBridge.exposeInMainWorld`, one explicit method per capability.
 *
 * This file is `.cts`, not `.ts`, on purpose: a sandboxed preload cannot be an
 * ES module, so it must emit CommonJS even though the rest of MAIN is ESM
 * (`REMEMBER.md § 5`). Renaming it back would silently break the window.
 *
 * Scaffold only — the exact 6-namespace surface lands in P0-04.
 */

export {};
