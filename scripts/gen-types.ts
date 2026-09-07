/**
 * Generates `packages/shared/src/api.ts` from the Pydantic models in
 * `core/aegis_core/server/schemas.py`.
 *
 * Type-safety rule (ARCHITECTURE.md § 4): never hand-write a TS type that
 * mirrors a Python model. CI fails if the generated file is stale.
 *
 * Scaffold only — implemented in P0-11.
 */

export {};
