import { create } from 'zustand';
import type { AegisBridge } from '@aegis/shared';

/**
 * What the main window knows about the kill switch itself (P3-14), apart from the
 * presses, which arrive on the stream.
 *
 * - `armed`: whether Windows gave Aegis the shortcut. MAIN is asked once, when the
 *   window opens; it can only change through a rebind, which answers for itself.
 * - `dismissedAt`: the `at` of the stop the person closed the notice for. A newer
 *   press shows again.
 */

export type KillSwitchArmed =
  | { readonly kind: 'unknown' }
  | { readonly kind: 'armed' }
  | { readonly kind: 'unarmed'; readonly message: string };

export interface KillSwitchState {
  readonly armed: KillSwitchArmed;
  readonly dismissedAt: string | null;
}

export const INITIAL_KILL_SWITCH: KillSwitchState = {
  armed: { kind: 'unknown' },
  dismissedAt: null,
};

export interface KillSwitchStore extends KillSwitchState {
  readonly dismiss: (at: string) => void;
}

export const useKillSwitchStore = create<KillSwitchStore>()((set) => ({
  ...INITIAL_KILL_SWITCH,
  dismiss: (at) => {
    set({ dismissedAt: at });
  },
}));

/**
 * Ask MAIN whether the kill switch is armed. `failed` is MAIN's refusal to show a
 * binding that does nothing, which is exactly "unarmed"; `unavailable` (no hotkey
 * service, or no bridge at all in Storybook) says nothing either way.
 */
export async function checkKillSwitch(
  bridge: Pick<AegisBridge, 'hotkeys'> | undefined = typeof window === 'undefined'
    ? undefined
    : window.aegis,
  store: { setState: (patch: Partial<KillSwitchState>) => void } = useKillSwitchStore,
): Promise<void> {
  if (bridge === undefined) return;
  try {
    const result = await bridge.hotkeys.get();
    if (result.ok) store.setState({ armed: { kind: 'armed' } });
    else if (result.error.code === 'failed') {
      store.setState({ armed: { kind: 'unarmed', message: result.error.message } });
    }
  } catch {
    // A bridge that throws is a bug in the bridge; the bar stays silent, not wrong.
  }
}
