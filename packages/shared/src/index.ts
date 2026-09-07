/**
 * @aegis/shared — types shared between the Electron MAIN process and the renderer.
 *
 * Type-safety rule (ARCHITECTURE.md § 4): Pydantic models in
 * `core/aegis_core/server/schemas.py` are the single source of truth. Anything
 * mirroring a Python model belongs in the generated `./api.ts` (P0-11) and must
 * never be hand-written here.
 */

export * from './api.js';

/** Placeholder until P0-11 generates the real surface. */
export const SHARED_PACKAGE_NAME = '@aegis/shared';
