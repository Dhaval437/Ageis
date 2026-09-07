/**
 * Spawns and supervises `aegis-core.exe`.
 *
 * Implements the startup handshake in ARCHITECTURE.md § 3.1 — ephemeral port,
 * session token over a stdin pipe (never argv), one JSON line on stdout, health
 * check, 3-strike respawn — and guarantees the core never outlives the UI.
 *
 * Scaffold only — implemented in P0-06 / P0-07.
 */

export {};
