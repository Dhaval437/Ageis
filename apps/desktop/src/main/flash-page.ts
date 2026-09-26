/**
 * The kill switch's edge flash (`UI.md § 7`, P3-14): the page one flash window
 * shows. Pure, so what it contains is testable without Electron.
 *
 * No script at all — the windows are created with JavaScript disabled — so the
 * fade is a CSS animation, and `prefers-reduced-motion` turns it into a static
 * border that simply disappears with the window (`UI.md § 10`).
 */

/** `UI.md § 7`: a 400 ms flash. */
export const FLASH_MS = 400;

/**
 * The longest a flash window may exist, painted or not. A window that never
 * paints must still never be left over the person's screen.
 */
export const FLASH_MAX_MS = 1_500;

/** `--danger` in the dark palette (`UI.md § 2`); readable on light and dark desktops. */
const DANGER = '#ff5c5c';

export function flashPageHtml(): string {
  return [
    '<!doctype html><html><head><meta charset="utf-8">',
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'">',
    '<style>',
    'html,body{margin:0;height:100%;overflow:hidden;background:transparent}',
    `body{box-sizing:border-box;box-shadow:inset 0 0 0 6px ${DANGER},inset 0 0 64px 16px ${DANGER}99;`,
    `animation:fade ${String(FLASH_MS)}ms ease-out forwards}`,
    '@keyframes fade{from{opacity:1}to{opacity:0}}',
    '@media (prefers-reduced-motion: reduce){body{animation:none}}',
    '</style></head><body></body></html>',
  ].join('');
}

/** The page as a `data:` URL, so the flash needs no file and no preload. */
export function flashPageUrl(): string {
  return `data:text/html;charset=utf-8,${encodeURIComponent(flashPageHtml())}`;
}
