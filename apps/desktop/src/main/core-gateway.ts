/**
 * The only thing in Aegis that talks HTTP to the core.
 *
 * This is the other half of "the renderer names a path, never a URL"
 * (ARCHITECTURE.md § 9.3): the origin, the port, the `/v1` prefix and the
 * bearer token are all added here, in MAIN, so a renderer that could name its
 * own host cannot exfiltrate the session token.
 *
 * Three rules hold for every request:
 *  - **No `Origin` header.** The core rejects any request that carries one — an
 *    `Origin` means a browser found the port. Node's fetch does not add one;
 *    nothing here may either.
 *  - **Every call has a timeout.** A hung core must surface as a failed request,
 *    not as a UI that waits forever (REVIEW.md § 2).
 *  - **The token is never logged, never returned, never put in an error.**
 *
 * `grantedRequest` is `request` plus MAIN's signature (`GRANT_HEADER`). Only MAIN's own
 * code calls it — the scope handlers, after the person picked a folder in the dialog —
 * never the renderer's generic `core.request` (P3-17).
 */

import { createHmac } from 'node:crypto';
import type { CoreRequest, CoreResponse } from '@aegis/shared';
import type { CoreGateway } from './bridge-handlers.js';

/** Loopback only. The core is never reachable from off the machine. */
const LOOPBACK = '127.0.0.1';

/** Every route in `ARCHITECTURE.md § 9.1` lives under this prefix. */
const API_PREFIX = '/v1';

/** Long enough for a slow local call, short enough that a wedged core shows up. */
const DEFAULT_TIMEOUT_MS = 10_000;

/**
 * Nothing the core returns today is remotely this big. The cap is here so a
 * runaway response cannot grow MAIN's heap without bound (REVIEW.md § 2).
 */
const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;

/**
 * MAIN's signature on a request only MAIN may make (P3-17): the core's scope routes
 * that add a folder require it, because the renderer's `core.request` arrives with the
 * same bearer token as everything else and cannot be told apart by it.
 */
export const GRANT_HEADER = 'x-aegis-grant';

/**
 * HMAC-SHA256 (hex) of `METHOD\nTARGET\nBODY`, keyed with the session token — the
 * core's `server/auth.py` `grant_signature`, and a test holds the two to one vector.
 * `target` is the path as sent, `/v1` prefix and any query included.
 */
export function grantSignature(
  token: string,
  method: string,
  target: string,
  body: string,
): string {
  return createHmac('sha256', token)
    .update(`${method.toUpperCase()}\n${target}\n`)
    .update(body)
    .digest('hex');
}

export type FetchFn = typeof globalThis.fetch;

export interface CoreGatewayOptions {
  readonly port: number;
  /** The session token from the handshake. Held here and nowhere else. */
  readonly token: string;
  readonly timeoutMs?: number;
  /** Injected in tests; defaults to the runtime's `fetch`. */
  readonly fetchImpl?: FetchFn;
}

async function readBody(response: Response): Promise<unknown> {
  const declared = response.headers.get('content-length');
  if (declared !== null && Number(declared) > MAX_RESPONSE_BYTES) {
    throw new Error('The core sent a response that is too large to read.');
  }
  const text = await response.text();
  if (text.length === 0) return null;
  if (text.length > MAX_RESPONSE_BYTES) {
    throw new Error('The core sent a response that is too large to read.');
  }
  try {
    return JSON.parse(text);
  } catch {
    // A non-JSON body from our own core means something is badly wrong, and
    // the raw text may hold anything. Report the shape, not the content.
    throw new Error('The core sent a response Aegis could not read.');
  }
}

/**
 * Build the authenticated gateway for one core session.
 *
 * A new one is made per core process: the token dies with the process that was
 * given it, so a respawn (P0-07) replaces the gateway rather than reusing it.
 */
export function createCoreGateway(options: CoreGatewayOptions): CoreGateway {
  const { port, token } = options;
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS;
  const doFetch = options.fetchImpl ?? globalThis.fetch;
  const origin = `http://${LOOPBACK}:${port}`;

  async function send(request: CoreRequest, signed: boolean): Promise<CoreResponse> {
    const headers: Record<string, string> = {
      authorization: `Bearer ${token}`,
      accept: 'application/json',
    };
    // Serialised once, so the bytes signed are exactly the bytes sent.
    const body = request.body === undefined ? undefined : JSON.stringify(request.body);
    if (body !== undefined) headers['content-type'] = 'application/json';
    const target = `${API_PREFIX}${request.path}`;
    if (signed) headers[GRANT_HEADER] = grantSignature(token, request.method, target, body ?? '');

    let response: Response;
    try {
      response = await doFetch(`${origin}${target}`, {
        method: request.method,
        headers,
        ...(body !== undefined ? { body } : {}),
        signal: AbortSignal.timeout(timeoutMs),
        // The core is not a browser origin and must never be treated as one.
        redirect: 'error',
      });
    } catch (error: unknown) {
      // The URL carries no secret, but the token is in the headers of the
      // request that failed — so nothing structured from `error` is passed on.
      throw new Error(
        error instanceof Error && error.name === 'TimeoutError'
          ? 'The core did not respond in time.'
          : 'Aegis could not reach its core.',
      );
    }

    return { status: response.status, body: await readBody(response) };
  }

  return {
    request: (request) => send(request, false),
    grantedRequest: (request) => send(request, true),
  };
}
