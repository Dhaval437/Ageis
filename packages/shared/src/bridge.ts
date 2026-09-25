/**
 * The preload bridge contract — the complete surface the renderer can reach
 * (`ARCHITECTURE.md § 9.3`).
 *
 * Hand-written on purpose, and the one exception to the "never hand-write a TS
 * type" rule in `ARCHITECTURE.md § 4`: none of this mirrors a Pydantic model.
 * Everything here is owned by the Electron MAIN process — windows, hotkeys,
 * updates, the shell — or is a deliberately opaque envelope around something
 * the core owns. Anything that *is* a core model stays `unknown` here and gets
 * its real type from the generated `./api.ts` when P0-11 lands.
 *
 * **Adding a member to `AegisBridge` is a security review item** (`REVIEW.md
 * § 5`). The renderer displays text the agent scraped off the user's screen, so
 * it is treated as a hostile caller: every method below is explicitly
 * enumerated, explicitly validated in MAIN, and there is no generic `invoke`.
 */

/** Why a bridge call did not succeed. Never a stack trace, never a raw path. */
export type BridgeErrorCode =
  /** The subsystem behind this call is not running yet (core down, feature unbuilt). */
  | 'unavailable'
  /** The renderer sent something malformed. Always a bug in the renderer. */
  | 'invalid_request'
  /** The path is outside everything the user has granted. */
  | 'not_granted'
  /** The operation ran and failed. */
  | 'failed';

export interface BridgeError {
  readonly code: BridgeErrorCode;
  readonly message: string;
}

/**
 * Bridge calls resolve, they do not reject: an `Error` thrown across
 * `contextBridge` arrives at the renderer as a bare string with its structure
 * gone, which makes "the core is not up yet" indistinguishable from "the user
 * denied it". Every failure is a value.
 */
export type BridgeResult<T> =
  { readonly ok: true; readonly value: T } | { readonly ok: false; readonly error: BridgeError };

/** Undoes a `subscribe`/`on*` registration. Always call it from a cleanup path. */
export type Unsubscribe = () => void;

export type CoreMethod = 'GET' | 'POST' | 'PUT' | 'DELETE';

/**
 * A request against the core's REST API (`ARCHITECTURE.md § 9.1`).
 *
 * The renderer supplies a **path**, never a URL: the origin, the port and the
 * session token are added in MAIN. That is the whole point of proxying — a
 * renderer that could name its own host could exfiltrate the session token.
 */
export interface CoreRequest {
  readonly method: CoreMethod;
  /** Rooted at the core's `/v1` prefix, e.g. `/health` or `/tasks/abc/steps`. */
  readonly path: string;
  readonly body?: unknown;
}

export interface CoreResponse {
  readonly status: number;
  /** Parsed JSON. Typed by the caller against the generated `./api.ts`. */
  readonly body: unknown;
}

/**
 * Where MAIN's connection to the event stream stands. Pushed, never polled.
 *
 * - `connecting` — a core is running and MAIN is (re)opening the stream.
 * - `live` — events are flowing.
 * - `down` — no core right now; the supervisor is starting or restarting one.
 * - `unavailable` — the supervisor gave up (`RECOVERY.md § 4`, P0-17's screen).
 */
export type CoreConnection = 'connecting' | 'live' | 'down' | 'unavailable';

/**
 * What `core.subscribe` delivers. An envelope, because the renderer has to learn
 * three things over the one channel and a bare event can only say one of them.
 */
export type CoreStreamMessage =
  /** One `§ 9.2` event, in `seq` order. `unknown` on purpose: the store validates it. */
  | { readonly kind: 'event'; readonly event: unknown }
  /**
   * Drop everything derived from the stream. Sent when a new core starts, when the
   * core cannot replay from MAIN's cursor, and when the renderer (re)loads. A full
   * replay of whatever the core retains follows.
   */
  | { readonly kind: 'reset' }
  | { readonly kind: 'connection'; readonly state: CoreConnection };

/** Global shortcuts, owned by MAIN so a hung core cannot disable them. */
export interface HotkeyMap {
  /** Electron accelerator for the kill switch (`REMEMBER.md` invariant 2). */
  readonly killSwitch: string;
}

export interface UpdateStatus {
  readonly state: 'idle' | 'checking' | 'available' | 'downloading' | 'ready' | 'error';
  /** The version being offered, when one is. */
  readonly version: string | null;
  /** Human-readable detail for the Settings screen. */
  readonly message: string | null;
}

/**
 * `window.aegis`. Six namespaces, nothing else.
 */
export interface AegisBridge {
  readonly core: {
    /** Proxied to the core over `127.0.0.1`; MAIN adds the bearer token. */
    request(request: CoreRequest): Promise<BridgeResult<CoreResponse>>;
    /**
     * The live event stream (`ARCHITECTURE.md § 9.2`), held open by MAIN. Events
     * arrive wrapped in a `CoreStreamMessage`; their bodies stay `unknown` and are
     * validated by the renderer's store.
     */
    subscribe(listener: (message: CoreStreamMessage) => void): Unsubscribe;
    /**
     * Starts the core again after the supervisor gave up (`RECOVERY.md § 4`,
     * P0-17). Takes no argument: the renderer asks for a restart, it never says
     * what to run — the launch spec lives in MAIN.
     *
     * Resolves `ok` once the core is **running**, and fails if the attempts are
     * spent again — resolving either way would tell the screen the engine is
     * back while it is still looking at a dead one. A restart already in flight
     * is joined, not started twice.
     */
    restart(): Promise<BridgeResult<null>>;
  };

  readonly window: {
    minimize(): void;
    /**
     * Maximises the window, or restores it if it is already maximised (P0-15).
     * One toggle rather than a pair, because the `□` button is one button and
     * MAIN owns the window state either way.
     */
    maximize(): void;
    close(): void;
    /**
     * Whether the window is maximised right now, pushed on every change —
     * including the ones the renderer did not cause: double-clicking the drag
     * region, `Win`+`↑`, and Aero snap all maximise a frameless window. Without
     * this the `□`/`❐` button would show a state it only guessed at.
     *
     * MAIN also pushes the current value when the page finishes loading, so a
     * listener registered before the first render always gets an answer.
     */
    onMaximizedChange(listener: (maximized: boolean) => void): Unsubscribe;
    /** Shows or hides the `OverlayHUD` window (P3-13). */
    setOverlay(visible: boolean): Promise<BridgeResult<null>>;
  };

  readonly hotkeys: {
    get(): Promise<BridgeResult<HotkeyMap>>;
    /** Resolves with the map actually in force, which may differ if a binding was rejected. */
    set(hotkeys: HotkeyMap): Promise<BridgeResult<HotkeyMap>>;
  };

  readonly system: {
    /** Opens the OS folder picker. Resolves `null` if the user cancelled. */
    pickFolder(): Promise<BridgeResult<string | null>>;
    /** Opens a path with its default handler. Only paths the user has granted. */
    openPath(path: string): Promise<BridgeResult<null>>;
    /** Reveals a path in Explorer with the item selected. Only paths the user has granted. */
    revealInExplorer(path: string): Promise<BridgeResult<null>>;
  };

  readonly updates: {
    check(): Promise<BridgeResult<UpdateStatus>>;
    install(): Promise<BridgeResult<null>>;
    onStatus(listener: (status: UpdateStatus) => void): Unsubscribe;
  };

  readonly app: {
    version(): Promise<string>;
    logsPath(): Promise<string>;
    /**
     * Puts `RECOVERY.md § 4`'s diagnostic report on the clipboard: versions, the
     * engine's state, and the last 200 log lines with the user's home paths and
     * anything key-shaped redacted (P0-17).
     *
     * MAIN writes the clipboard itself rather than handing the text back,
     * because log lines will carry text the agent scraped off the user's screen
     * once P2 lands, and the renderer has no reason to hold it.
     */
    copyDiagnosticReport(): Promise<BridgeResult<null>>;
    onDeepLink(listener: (url: string) => void): Unsubscribe;
  };
}

/**
 * `window.aegisHud` — the OverlayHUD window's whole surface (P3-13), from its own
 * preload (`preload/hud.cts`). **Deliberately far narrower than `AegisBridge`.**
 *
 * The HUD is a second renderer that shows the same untrusted text the main window
 * does, and it sits over the apps the agent is driving. It gets exactly what it
 * needs and nothing a compromised page could widen: no generic core request, no
 * files, no hotkeys. It can stop the agent and it can *deny* a pending approval —
 * both of which only ever make Aegis do less — and it can bring the main window
 * forward. It can never allow anything: an approval is allowed only in the dialog,
 * behind its input guard.
 */
export interface AegisHudBridge {
  /** The same stream the main window gets (`ARCHITECTURE.md § 9.2`). */
  subscribe(listener: (message: CoreStreamMessage) => void): Unsubscribe;
  /** Presses the kill switch (`P3-06`): the same path as the hotkey and the tray. */
  stop(): void;
  /** Brings the main window forward — the HUD's expand control. */
  showMain(): void;
  /** Denies one pending approval. There is no `allow`. */
  deny(approvalId: number): Promise<BridgeResult<null>>;
}
