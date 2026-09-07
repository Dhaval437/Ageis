/**
 * Global shortcuts, including the kill switch.
 *
 * The kill switch lives here, in MAIN, precisely so a hung Python core cannot
 * disable it (REMEMBER.md invariant 2). Every abort path must also release
 * Ctrl/Alt/Shift/Win (invariant 3).
 *
 * Scaffold only — implemented in P3.
 */

export {};
