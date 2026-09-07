/**
 * Electron MAIN entry point.
 *
 * MAIN is the only process the user sees, and it owns the things that must keep
 * working when everything else is wedged: the kill-switch hotkey (REMEMBER.md
 * invariant 2) and the supervision of the Python core (invariant 14).
 *
 * Scaffold only — the real window, tray and single-instance lock land in P0-02.
 */

export function main(): void {
  // P0-02: app.whenReady() → create window, tray, single-instance lock.
}
