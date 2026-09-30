import type { AegisApprovalBridge, AegisBridge, AegisHudBridge } from '@aegis/shared';

/**
 * `window.aegis` — everything the renderer can reach (`ARCHITECTURE.md § 9.3`).
 *
 * Non-optional on purpose. It is injected by the preload before any renderer
 * code runs, so a missing bridge means the window is broken, and code that
 * defends against `undefined` here would hide that instead of surfacing it.
 */
declare global {
  interface Window {
    readonly aegis: AegisBridge;
    /**
     * The OverlayHUD's narrow surface (P3-13). Present only in the HUD window, whose
     * preload is `hud.cts`; the main window never has it, hence optional.
     */
    readonly aegisHud?: AegisHudBridge;
    /**
     * The approval window's two-member surface (P3-19). Present only in that window,
     * whose preload is `approval.cts`.
     */
    readonly aegisApproval?: AegisApprovalBridge;
  }
}

export {};
