import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BridgeResult, CoreRequest, CoreResponse } from '@aegis/shared';
import { CATALOG, CONFIGURED, SPEND } from '../.storybook/models-fixtures';
import { loadModels, removeKey, saveKey, saveSettings, testProvider } from '@/lib/models-client';
import { INITIAL_MODELS, useModelsStore } from '@/stores/models';

/**
 * Everything the Models screen asks the core to do, and what it says when the
 * core refuses.
 *
 * The one that matters most is the last block: a key goes out through exactly
 * one call and is gone from the renderer the moment it has.
 */

const FAKE_KEY = 'sk-proj-0123456789abcdefghijklmnop';

type Reply = { status: number; body: unknown };

/** Answers by method and path, the way the core does. */
class Core {
  readonly requests: CoreRequest[] = [];
  private readonly replies = new Map<string, Reply>();
  private down = false;

  constructor() {
    this.on('GET /models/catalog', 200, CATALOG);
    this.on('GET /settings', 200, { models: CONFIGURED });
    this.on('GET /models/spend', 200, SPEND);
    this.on('PUT /settings', 200, { models: CONFIGURED });
    this.on('PUT /models/keys/openai', 200, {
      provider_id: 'openai',
      has_key: true,
      masked_key: 'sk-…mnop',
    });
    this.on('DELETE /models/keys/openai', 200, {
      provider_id: 'openai',
      has_key: false,
      masked_key: null,
    });
    this.on('POST /models/validate', 200, {
      provider_id: 'openai',
      valid: true,
      detail: 'That key works.',
      latency_ms: 42,
    });
  }

  on(route: string, status: number, body: unknown): void {
    this.replies.set(route, { status, body });
  }

  goDown(): void {
    this.down = true;
  }

  request = (request: CoreRequest): Promise<BridgeResult<CoreResponse>> => {
    this.requests.push(request);
    if (this.down) {
      return Promise.resolve({
        ok: false,
        error: { code: 'unavailable', message: 'no core' },
      });
    }
    const reply = this.replies.get(`${request.method} ${request.path}`);
    if (reply === undefined) {
      return Promise.resolve({ ok: true, value: { status: 404, body: { detail: 'not found' } } });
    }
    return Promise.resolve({ ok: true, value: { status: reply.status, body: reply.body } });
  };
}

let core: Core;

beforeEach(() => {
  vi.clearAllMocks();
  core = new Core();
  useModelsStore.setState(INITIAL_MODELS);
  vi.stubGlobal('aegis', { core: { request: core.request } });
});

describe('loadModels', () => {
  it('fills the screen from the three reads it needs', async () => {
    await loadModels();
    const state = useModelsStore.getState();
    expect(state.status).toBe('ready');
    expect(state.catalog).toEqual(CATALOG);
    expect(state.saved).toEqual(CONFIGURED);
    expect(state.draft).toEqual(CONFIGURED);
    expect(state.spend).toEqual(SPEND);
  });

  it('starts the draft equal to what is saved, so nothing looks unsaved', async () => {
    await loadModels();
    const state = useModelsStore.getState();
    expect(state.draft).toEqual(state.saved);
  });

  it('fails the whole screen when the core is not running', async () => {
    core.goDown();
    await loadModels();
    const state = useModelsStore.getState();
    expect(state.status).toBe('error');
    expect(state.error).toContain('cannot reach the engine');
  });

  it('fails rather than rendering half a catalogue', async () => {
    core.on('GET /models/catalog', 200, { providers: [{ provider_id: 'nope' }] });
    await loadModels();
    expect(useModelsStore.getState().status).toBe('error');
  });
});

describe('saveSettings', () => {
  it('stores what the core answers with, not what was sent', async () => {
    await loadModels();
    const trimmed = { ...CONFIGURED, custom_base_url: 'https://gateway.example/v1' };
    core.on('PUT /settings', 200, { models: trimmed });
    await saveSettings({ ...CONFIGURED, custom_base_url: '  https://gateway.example/v1  ' });
    const state = useModelsStore.getState();
    expect(state.saved).toEqual(trimmed);
    expect(state.draft).toEqual(trimmed);
    expect(state.notice).toEqual({ tone: 'ok', text: 'Saved.' });
  });

  it('shows the core’s own sentence when it refuses', async () => {
    await loadModels();
    core.on('PUT /settings', 400, { detail: 'a role names the same model more than once' });
    await saveSettings(CONFIGURED);
    const state = useModelsStore.getState();
    expect(state.notice).toEqual({
      tone: 'error',
      text: 'a role names the same model more than once',
    });
    expect(state.saving).toBe(false);
  });

  it('never shows a 5xx body, which is written for a developer', async () => {
    await loadModels();
    core.on('PUT /settings', 500, { detail: 'Traceback (most recent call last)' });
    await saveSettings(CONFIGURED);
    expect(useModelsStore.getState().notice?.text).not.toContain('Traceback');
  });

  it('keeps the draft when the save fails, so nothing the user typed is lost', async () => {
    await loadModels();
    const edited = { ...CONFIGURED, custom_base_url: 'http://example.com/v1' };
    useModelsStore.getState().edit(() => edited);
    core.on('PUT /settings', 400, { detail: 'Use https:// for an address off this machine.' });
    await saveSettings(edited);
    expect(useModelsStore.getState().draft).toEqual(edited);
  });
});

describe('testProvider', () => {
  it('records the result against that provider', async () => {
    await testProvider('openai');
    const state = useModelsStore.getState();
    expect(state.testing).toBeNull();
    expect(state.results.openai?.valid).toBe(true);
    expect(state.results.openai?.latency_ms).toBe(42);
  });

  it('shows a failed key as a result rather than as a broken screen', async () => {
    core.on('POST /models/validate', 200, {
      provider_id: 'openai',
      valid: false,
      detail: 'The provider rejected that key.',
      latency_ms: 300,
    });
    await testProvider('openai');
    expect(useModelsStore.getState().results.openai?.valid).toBe(false);
    expect(useModelsStore.getState().status).not.toBe('error');
  });

  it('says so when the engine cannot be reached at all', async () => {
    core.goDown();
    await testProvider('openai');
    expect(useModelsStore.getState().notice?.tone).toBe('error');
  });
});

describe('keys', () => {
  it('sends a key exactly once, to one route, and never asks for one back', async () => {
    await loadModels();
    useModelsStore.getState().typeKey('openai', FAKE_KEY);
    await saveKey('openai', FAKE_KEY);

    const carrying = core.requests.filter((request) => JSON.stringify(request).includes(FAKE_KEY));
    expect(carrying).toHaveLength(1);
    expect(carrying[0]?.method).toBe('PUT');
    expect(carrying[0]?.path).toBe('/models/keys/openai');
    expect(
      core.requests.some((request) => request.method === 'GET' && request.path.includes('keys')),
    ).toBe(false);
  });

  it('clears the field once the key is saved, so it lives nowhere in the renderer', async () => {
    await loadModels();
    useModelsStore.getState().typeKey('openai', FAKE_KEY);
    await saveKey('openai', FAKE_KEY);
    const state = useModelsStore.getState();
    expect(state.keyDrafts.openai).toBeUndefined();
    expect(JSON.stringify(state)).not.toContain(FAKE_KEY);
    expect(state.notice).toEqual({ tone: 'ok', text: 'Key saved.' });
  });

  it('keeps what was typed when the core refuses it, and shows why', async () => {
    await loadModels();
    useModelsStore.getState().typeKey('openai', FAKE_KEY);
    core.on('PUT /models/keys/openai', 400, {
      detail: 'That key contains characters an API key cannot contain.',
    });
    await saveKey('openai', FAKE_KEY);
    const state = useModelsStore.getState();
    expect(state.keyDrafts.openai).toBe(FAKE_KEY);
    expect(state.notice?.text).toContain('characters an API key cannot contain');
  });

  it('reloads the catalogue after a write, so the mask on screen is current', async () => {
    await loadModels();
    const withKey = {
      providers: CATALOG.providers.map((provider) =>
        provider.provider_id === 'openai'
          ? { ...provider, has_key: true, masked_key: 'sk-…wxyz' }
          : provider,
      ),
    };
    core.on('GET /models/catalog', 200, withKey);
    await saveKey('openai', FAKE_KEY);
    expect(useModelsStore.getState().catalog?.providers[0]?.masked_key).toBe('sk-…wxyz');
  });

  it('removes a key without sending one', async () => {
    await loadModels();
    await removeKey('openai');
    const remove = core.requests.find((request) => request.method === 'DELETE');
    expect(remove?.path).toBe('/models/keys/openai');
    expect(remove?.body).toBeUndefined();
    expect(useModelsStore.getState().notice).toEqual({ tone: 'ok', text: 'Key removed.' });
  });
});
