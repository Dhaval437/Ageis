/**
 * MAIN's own keyboard, for the one thing only MAIN can still do when the core is
 * gone: let go of the modifier keys (`RECOVERY.md § 4`, P3-15).
 *
 * The core's `SendInput` wrapper cannot help a dead core, and Windows keeps an
 * injected `KEYDOWN` down after the process that sent it dies — a `Ctrl` left held
 * by a hung agent stays held on the user's machine. So MAIN carries two Win32 calls
 * of its own, through `koffi` (a prebuilt N-API FFI; no compiler, no install step):
 *
 * - `GetAsyncKeyState`, to see which modifiers are down;
 * - `SendInput`, to send a `KEYUP` for them — built exactly as the core builds one:
 *   the scan code `MapVirtualKeyW` gives, `KEYEVENTF_EXTENDEDKEY` for the keys
 *   Microsoft documents as extended, and `AEGIS_SIGNATURE` in `dwExtraInfo`, so
 *   nothing that watches for Aegis' own events mistakes it for the human.
 *
 * It also carries the two calls the approval window needs to hand the focus back
 * (P3-19): `GetForegroundWindow`, to note who had it before the dialog took it, and
 * `SetForegroundWindow`, to return it when the dialog goes. A hidden window that
 * still holds the focus would swallow the person's next keystrokes.
 *
 * A child process was measured and rejected: the venv's Python takes ~95 ms to start
 * and PowerShell ~257 ms, and the kill switch's whole budget is 200 ms. A call
 * through here takes well under a microsecond.
 *
 * This is the only file in MAIN that loads `koffi`, and it does so lazily, on Windows
 * only, so a test that never needs the real keyboard never loads a native module.
 */

import { createRequire } from 'node:module';
import type * as KoffiModule from 'koffi';

type Koffi = typeof KoffiModule;

/** `actuation/signature.py`'s `AEGIS_SIGNATURE`: every event Aegis sends carries it. */
export const AEGIS_SIGNATURE = 0xae615000;

const INPUT_KEYBOARD = 1;
const KEYEVENTF_EXTENDEDKEY = 0x0001;
const KEYEVENTF_KEYUP = 0x0002;
const MAPVK_VK_TO_VSC_EX = 4;
const PROCESS_QUERY_LIMITED_INFORMATION = 0x1000;
const TOKEN_QUERY = 0x0008;
const TOKEN_INTEGRITY_LEVEL = 25;

/** Right Ctrl, right Alt and both Windows keys: the extended modifiers. */
const EXTENDED = new Set([0xa3, 0xa5, 0x5b, 0x5c]);

export interface KeyInput {
  /** Whether Windows reports the key down right now (physical or injected). */
  readonly isDown: (vk: number) => boolean;
  /** Sends one tagged `KEYUP`. Throws if Windows refused it. */
  readonly keyUp: (vk: number) => void;
}

export interface InputBarrier {
  /** Whether UIPI can drop MAIN's input to the window in front. */
  readonly blocked: () => boolean;
}

/** A window handle, as Windows gives it. Opaque: only ever handed back to Windows. */
export type WindowHandle = number | bigint;

export interface ForegroundWindows {
  /** The window the person is working in right now, or `null` if there is none. */
  readonly current: () => WindowHandle | null;
  /** Ask Windows to bring `handle` to the front. `false` if it refused. */
  readonly restore: (handle: WindowHandle) => boolean;
}

interface Native {
  GetForegroundWindow: () => WindowHandle;
  SetForegroundWindow: (handle: WindowHandle) => number;
  foregroundOutranks: () => boolean;
  GetAsyncKeyState: (vk: number) => number;
  MapVirtualKeyW: (code: number, mapType: number) => number;
  SendInput: (count: number, inputs: unknown[], size: number) => number;
  inputSize: number;
}

let native: Native | null | undefined;

function load(): Native | null {
  if (native !== undefined) return native;
  if (process.platform !== 'win32') return (native = null);
  try {
    const require = createRequire(import.meta.url);
    const koffi = require('koffi') as Koffi;
    const user32 = koffi.load('user32.dll');
    const kernel32 = koffi.load('kernel32.dll');
    const advapi32 = koffi.load('advapi32.dll');
    const KEYBDINPUT = koffi.struct('AEGIS_KEYBDINPUT', {
      wVk: 'uint16',
      wScan: 'uint16',
      dwFlags: 'uint32',
      time: 'uint32',
      dwExtraInfo: 'uintptr_t',
    });
    // The union's size is its largest member, MOUSEINPUT; declared so `INPUT` is 40
    // bytes on x64, the `cbSize` `SendInput` checks.
    const MOUSEINPUT = koffi.struct('AEGIS_MOUSEINPUT', {
      dx: 'int32',
      dy: 'int32',
      mouseData: 'uint32',
      dwFlags: 'uint32',
      time: 'uint32',
      dwExtraInfo: 'uintptr_t',
    });
    const UNION = koffi.union('AEGIS_INPUT_UNION', { mi: MOUSEINPUT, ki: KEYBDINPUT });
    const INPUT = koffi.struct('AEGIS_INPUT', { type: 'uint32', u: UNION });
    const GetForegroundWindow = user32.func('uintptr_t __stdcall GetForegroundWindow()');
    const GetWindowThreadProcessId = user32.func(
      'uint32 __stdcall GetWindowThreadProcessId(uintptr_t hWnd, _Out_ uint32 *lpdwProcessId)',
    );
    const OpenProcess = kernel32.func(
      'uintptr_t __stdcall OpenProcess(uint32 dwDesiredAccess, int bInheritHandle, uint32 dwProcessId)',
    );
    const CloseHandle = kernel32.func('int __stdcall CloseHandle(uintptr_t hObject)');
    const OpenProcessToken = advapi32.func(
      'int __stdcall OpenProcessToken(uintptr_t ProcessHandle, uint32 DesiredAccess, _Out_ uintptr_t *TokenHandle)',
    );
    const GetTokenInformation = advapi32.func(
      'int __stdcall GetTokenInformation(uintptr_t TokenHandle, int TokenInformationClass, _Out_ void *TokenInformation, uint32 TokenInformationLength, _Out_ uint32 *ReturnLength)',
    );
    const GetSidSubAuthorityCount = advapi32.func(
      'uint8 * __stdcall GetSidSubAuthorityCount(const void *pSid)',
    );
    const GetSidSubAuthority = advapi32.func(
      'uint32 * __stdcall GetSidSubAuthority(const void *pSid, uint32 nSubAuthority)',
    );

    function processIntegrity(pid: number): number | null {
      const processHandle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid) as WindowHandle;
      if (Number(processHandle) === 0) return null;
      const tokenHandle: Array<WindowHandle | null> = [null];
      try {
        if (OpenProcessToken(processHandle, TOKEN_QUERY, tokenHandle) === 0) return null;
        const token = tokenHandle[0];
        if (token === null || Number(token) === 0) return null;
        try {
          const label = Buffer.alloc(64);
          const returned = [0];
          if (
            GetTokenInformation(token, TOKEN_INTEGRITY_LEVEL, label, label.length, returned) === 0
          ) {
            return null;
          }
          const sid = koffi.decode(label, 'uintptr_t') as WindowHandle;
          if (Number(sid) === 0) return null;
          const countPointer = GetSidSubAuthorityCount(sid) as WindowHandle;
          if (Number(countPointer) === 0) return null;
          const count = koffi.decode(countPointer, 'uint8') as number;
          if (count === 0) return null;
          const ridPointer = GetSidSubAuthority(sid, count - 1) as WindowHandle;
          if (Number(ridPointer) === 0) return null;
          return koffi.decode(ridPointer, 'uint32') as number;
        } finally {
          CloseHandle(token);
        }
      } finally {
        CloseHandle(processHandle);
      }
    }

    const ownIntegrity = processIntegrity(process.pid);
    native = {
      GetForegroundWindow,
      SetForegroundWindow: user32.func('int __stdcall SetForegroundWindow(uintptr_t hWnd)'),
      foregroundOutranks: () => {
        const window = GetForegroundWindow() as WindowHandle;
        if (Number(window) === 0) return true;
        const pid = [0];
        GetWindowThreadProcessId(window, pid);
        if (pid[0] === 0) return true;
        const foregroundIntegrity = processIntegrity(pid[0] ?? 0);
        return (
          foregroundIntegrity === null ||
          ownIntegrity === null ||
          foregroundIntegrity > ownIntegrity
        );
      },
      GetAsyncKeyState: user32.func('short __stdcall GetAsyncKeyState(int vKey)'),
      MapVirtualKeyW: user32.func('uint32 __stdcall MapVirtualKeyW(uint32 uCode, uint32 uMapType)'),
      SendInput: user32.func(
        'uint32 __stdcall SendInput(uint32 cInputs, const AEGIS_INPUT *pInputs, int cbSize)',
      ),
      inputSize: koffi.sizeof(INPUT),
    };
  } catch (error: unknown) {
    console.error('[main] the native keyboard could not be loaded', error);
    native = null;
  }
  return native;
}

/** Whether MAIN's input can reach the foreground window, or null off Windows. */
export function nativeInputBarrier(): InputBarrier | null {
  const api = load();
  if (api === null) return null;
  return { blocked: api.foregroundOutranks };
}

/** The real foreground window, or `null` where it cannot be asked for. */
export function nativeForeground(): ForegroundWindows | null {
  const api = load();
  if (api === null) return null;
  return {
    current: () => {
      const handle = api.GetForegroundWindow();
      return Number(handle) === 0 ? null : handle;
    },
    restore: (handle) => api.SetForegroundWindow(handle) !== 0,
  };
}

/** The real keyboard, or `null` where there is none to be had. */
export function nativeKeyInput(): KeyInput | null {
  const api = load();
  if (api === null) return null;
  return {
    // The high bit of `GetAsyncKeyState` is "down now".
    isDown: (vk) => (api.GetAsyncKeyState(vk) & 0x8000) !== 0,
    keyUp: (vk) => {
      const scan = api.MapVirtualKeyW(vk, MAPVK_VK_TO_VSC_EX);
      const flags =
        KEYEVENTF_KEYUP | (EXTENDED.has(vk) || scan >> 8 === 0xe0 ? KEYEVENTF_EXTENDEDKEY : 0);
      const input = {
        type: INPUT_KEYBOARD,
        u: {
          ki: {
            wVk: vk,
            wScan: scan & 0xff,
            dwFlags: flags,
            time: 0,
            dwExtraInfo: AEGIS_SIGNATURE,
          },
        },
      };
      if (api.SendInput(1, [input], api.inputSize) !== 1) {
        throw new Error(`SendInput refused the KEYUP for ${vk.toString(16)}`);
      }
    },
  };
}
