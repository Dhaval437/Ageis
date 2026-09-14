/**
 * @aegis/shared — types shared between the Electron MAIN process and the renderer.
 *
 * Type-safety rule (ARCHITECTURE.md § 4): Pydantic models in
 * `core/aegis_core/server/schemas.py` are the single source of truth. Anything
 * mirroring a Python model belongs in the generated `./api.ts` (`pnpm gen:types`)
 * and must never be hand-written here.
 */

export * from './api.js';

/**
 * The preload bridge contract. Hand-written and MAIN-owned, not generated —
 * see the note at the top of `./bridge.ts`.
 */
export * from './bridge.js';
