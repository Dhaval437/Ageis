/**
 * GENERATED FILE — do not edit by hand.
 *
 * `scripts/gen-types.ts` (task P0-11) regenerates this from the Pydantic models in
 * `core/aegis_core/server/schemas.py`. Until then it holds a single placeholder so
 * the package compiles.
 */

/** Risk tiers every tool call is classified into. See REMEMBER.md § 6. */
export type RiskTier = 'SAFE' | 'CAUTION' | 'DANGEROUS' | 'FORBIDDEN';
