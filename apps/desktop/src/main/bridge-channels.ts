/**
 * Every IPC channel that exists, in one list.
 *
 * MAIN registers exactly these and nothing else, so "what can the renderer
 * reach?" is answered by reading one file. The preload duplicates the strings
 * because a sandboxed preload cannot import a sibling module; `tests/bridge.test.ts`
 * asserts the two lists match exactly, in both directions.
 */

/** Renderer→MAIN, request/response. */
export const INVOKE_CHANNELS = {
  coreRequest: 'aegis:core:request',
  coreRestart: 'aegis:core:restart',
  windowSetOverlay: 'aegis:window:set-overlay',
  hotkeysGet: 'aegis:hotkeys:get',
  hotkeysSet: 'aegis:hotkeys:set',
  systemPickFolder: 'aegis:system:pick-folder',
  systemOpenPath: 'aegis:system:open-path',
  systemRevealInExplorer: 'aegis:system:reveal-in-explorer',
  updatesCheck: 'aegis:updates:check',
  updatesInstall: 'aegis:updates:install',
  appVersion: 'aegis:app:version',
  appLogsPath: 'aegis:app:logs-path',
  appCopyDiagnosticReport: 'aegis:app:copy-diagnostic-report',
} as const;

/** Renderer→MAIN, fire-and-forget. Nothing here may return data. */
export const SEND_CHANNELS = {
  windowMinimize: 'aegis:window:minimize',
  windowMaximize: 'aegis:window:maximize',
  windowClose: 'aegis:window:close',
} as const;

/** MAIN→renderer, pushed. The UI is a pure function of these (invariant 15). */
export const EVENT_CHANNELS = {
  coreEvent: 'aegis:core:event',
  windowMaximized: 'aegis:window:maximized',
  updatesStatus: 'aegis:updates:status',
  appDeepLink: 'aegis:app:deep-link',
} as const;

export type InvokeChannel = (typeof INVOKE_CHANNELS)[keyof typeof INVOKE_CHANNELS];
export type SendChannel = (typeof SEND_CHANNELS)[keyof typeof SEND_CHANNELS];
export type EventChannel = (typeof EVENT_CHANNELS)[keyof typeof EVENT_CHANNELS];

/** Every channel name the bridge is allowed to use. */
export const ALL_CHANNELS: readonly string[] = [
  ...Object.values(INVOKE_CHANNELS),
  ...Object.values(SEND_CHANNELS),
  ...Object.values(EVENT_CHANNELS),
];
