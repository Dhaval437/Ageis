/**
 * The preload bridge — the ONLY surface exposed to the renderer.
 *
 * The renderer is sandboxed with `nodeIntegration: false` and has zero fs/net
 * access (ARCHITECTURE.md § 3). Everything it can do is enumerated here via
 * `contextBridge.exposeInMainWorld`, one explicit method per capability.
 *
 * Scaffold only — the exact 6-namespace surface lands in P0-04.
 */

export {};
