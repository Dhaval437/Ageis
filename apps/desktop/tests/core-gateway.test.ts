import { describe, expect, it, vi } from 'vitest';
import { createCoreGateway, type FetchFn } from '../src/main/core-gateway.js';

const TOKEN = 'a'.repeat(64);
const PORT = 54321;

interface Captured {
  url: string;
  init: RequestInit;
}

function gatewayReturning(response: Response, captured: Captured[] = []) {
  const fetchImpl = ((url: string, init: RequestInit) => {
    captured.push({ url, init });
    return Promise.resolve(response);
  }) as unknown as FetchFn;
  return { gateway: createCoreGateway({ port: PORT, token: TOKEN, fetchImpl }), captured };
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });
}

function headersOf(init: RequestInit): Record<string, string> {
  return (init.headers ?? {}) as Record<string, string>;
}

describe('createCoreGateway', () => {
  it('adds the origin, the port and the /v1 prefix to the renderer path', async () => {
    const { gateway, captured } = gatewayReturning(jsonResponse({ status: 'ok' }));
    await gateway.request({ method: 'GET', path: '/health' });

    expect(captured[0]?.url).toBe(`http://127.0.0.1:${PORT}/v1/health`);
  });

  it('sends the session token as a bearer credential', async () => {
    const { gateway, captured } = gatewayReturning(jsonResponse({}));
    await gateway.request({ method: 'GET', path: '/health' });

    expect(headersOf(captured[0]!.init)['authorization']).toBe(`Bearer ${TOKEN}`);
  });

  it('never sends an Origin header, which the core rejects outright', async () => {
    const { gateway, captured } = gatewayReturning(jsonResponse({}));
    await gateway.request({ method: 'GET', path: '/health' });

    const names = Object.keys(headersOf(captured[0]!.init)).map((n) => n.toLowerCase());
    expect(names).not.toContain('origin');
  });

  it('returns the status and the parsed body', async () => {
    const { gateway } = gatewayReturning(jsonResponse({ status: 'ok', uptime: 1.5 }));
    const response = await gateway.request({ method: 'GET', path: '/health' });

    expect(response).toEqual({ status: 200, body: { status: 'ok', uptime: 1.5 } });
  });

  it('passes a 401 through rather than throwing', async () => {
    // A rejected credential is an answer, and MAIN's supervisor has to see it.
    const { gateway } = gatewayReturning(jsonResponse({ detail: 'unauthorized' }, 401));
    const response = await gateway.request({ method: 'GET', path: '/health' });

    expect(response.status).toBe(401);
  });

  it('serialises a body and declares its content type', async () => {
    const { gateway, captured } = gatewayReturning(jsonResponse({}));
    await gateway.request({ method: 'POST', path: '/tasks', body: { goal: 'tidy' } });

    expect(captured[0]?.init.body).toBe('{"goal":"tidy"}');
    expect(headersOf(captured[0]!.init)['content-type']).toBe('application/json');
  });

  it('sends no body and no content type when there is none', async () => {
    const { gateway, captured } = gatewayReturning(jsonResponse({}));
    await gateway.request({ method: 'GET', path: '/health' });

    expect(captured[0]?.init.body).toBeUndefined();
    expect(headersOf(captured[0]!.init)['content-type']).toBeUndefined();
  });

  it('reads an empty body as null', async () => {
    const { gateway } = gatewayReturning(new Response('', { status: 200 }));
    expect(await gateway.request({ method: 'GET', path: '/health' })).toEqual({
      status: 200,
      body: null,
    });
  });

  it('refuses to follow a redirect', async () => {
    const { gateway, captured } = gatewayReturning(jsonResponse({}));
    await gateway.request({ method: 'GET', path: '/health' });

    expect(captured[0]?.init.redirect).toBe('error');
  });

  it('gives every request a timeout', async () => {
    const { gateway, captured } = gatewayReturning(jsonResponse({}));
    await gateway.request({ method: 'GET', path: '/health' });

    expect(captured[0]?.init.signal).toBeInstanceOf(AbortSignal);
  });

  it('reports a timeout as a timeout', async () => {
    const timeout = new Error('timed out');
    timeout.name = 'TimeoutError';
    const fetchImpl = (() => Promise.reject(timeout)) as unknown as FetchFn;
    const gateway = createCoreGateway({ port: PORT, token: TOKEN, fetchImpl });

    await expect(gateway.request({ method: 'GET', path: '/health' })).rejects.toThrow(
      /did not respond in time/,
    );
  });

  it('never leaks the token through a transport error', async () => {
    const fetchImpl = (() =>
      Promise.reject(new Error(`connect ECONNREFUSED with Bearer ${TOKEN}`))) as unknown as FetchFn;
    const gateway = createCoreGateway({ port: PORT, token: TOKEN, fetchImpl });

    await expect(gateway.request({ method: 'GET', path: '/health' })).rejects.toThrow(
      /could not reach its core/,
    );
    await gateway.request({ method: 'GET', path: '/health' }).catch((error: unknown) => {
      expect(String(error)).not.toContain(TOKEN);
    });
  });

  it('rejects a body the core says is too large before reading it', async () => {
    const response = new Response('{}', {
      headers: { 'content-type': 'application/json', 'content-length': String(9 * 1024 * 1024) },
    });
    const { gateway } = gatewayReturning(response);

    await expect(gateway.request({ method: 'GET', path: '/health' })).rejects.toThrow(/too large/);
  });

  it('reports an unreadable body without echoing it', async () => {
    const { gateway } = gatewayReturning(new Response('<html>not our core</html>'));

    await expect(gateway.request({ method: 'GET', path: '/health' })).rejects.toThrow(
      /could not read/,
    );
  });

  it('uses the runtime fetch when none is injected', () => {
    const spy = vi.fn();
    const original = globalThis.fetch;
    globalThis.fetch = spy;
    try {
      createCoreGateway({ port: PORT, token: TOKEN });
    } finally {
      globalThis.fetch = original;
    }
    expect(spy).not.toHaveBeenCalled();
  });
});
