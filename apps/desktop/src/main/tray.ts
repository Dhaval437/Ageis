/**
 * The tray icon and its menu.
 *
 * The tray is the guarantee that a running Aegis is always reachable: as long as
 * the process is alive there is a visible way to show the window and to quit
 * (`REMEMBER.md` invariant 14 — no headless agent with a mouse).
 */

import { Menu, Tray, nativeImage } from 'electron';
import { buildTrayMenuTemplate, type TrayMenuActions, type TrayMenuState } from './tray-menu.js';

/**
 * A 32×32 placeholder mark in `--accent`, inlined so the skeleton needs no asset
 * pipeline. P7-02 replaces it with the real branded `.ico`.
 */
const TRAY_ICON_PNG =
  'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAcUlEQVR42u2Xyw0AIAhDGdDVnBkXMP4KSg1NvFH6LsYqkoquUlWR8ywYArEO34LwCl+GeArgHT6FoANAfRCApZcPwMvPAdCbR3ZwAYxmT/csA8xmkV2YyXKXdan4AwC951yvYTaiEKU0RC0P8TFJ3VIDvLDsxjkXNO4AAAAASUVORK5CYII=';

export interface TrayHandle {
  /** Rebuild the menu after something it displays has changed. */
  readonly refresh: (state: TrayMenuState) => void;
  readonly destroy: () => void;
}

export function createTray(initial: TrayMenuState, actions: TrayMenuActions): TrayHandle {
  const icon = nativeImage.createFromDataURL(TRAY_ICON_PNG);
  const tray = new Tray(icon);
  tray.setToolTip('Aegis');

  const refresh = (state: TrayMenuState): void => {
    tray.setContextMenu(Menu.buildFromTemplate(buildTrayMenuTemplate(state, actions)));
  };

  refresh(initial);
  tray.on('click', actions.toggleWindow);

  return {
    refresh,
    destroy: () => {
      tray.destroy();
    },
  };
}
