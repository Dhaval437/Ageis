import type {
  BridgeResult,
  CoreRequest,
  CoreResponse,
  ModelSettings,
  ProviderId,
} from '@aegis/shared';
import {
  coreDetail,
  parseCatalog,
  parseSettings,
  parseSpend,
  parseValidate,
} from '@/lib/models-api';
import { useModelsStore } from '@/stores/models';

/**
 * Everything the Models screen asks the core to do (`UI.md § 8.4`).
 *
 * Separate from the components so each outcome has a test, and so the copy the
 * user reads is in one place to be checked against `UI.md § 11`: what happened,
 * then what to do about it.
 *
 * Two rules hold throughout.
 *
 * **A key goes out and never comes back.** `saveKey` is the only function that
 * sends one; it clears the field by reloading the catalogue, which answers with
 * `masked_key` and nothing else.
 *
 * **The core's own words are shown only when the core wrote them for the user.**
 * A 4xx `detail` from `/v1/settings` or `/v1/models/keys/{id}` is a sentence the
 * core composed for this screen and quotes neither a key nor an address. Any
 * other failure is reported in our words, because a 500's body is written for a
 * developer.
 */

const UNREACHABLE = 'Aegis cannot reach the engine. Check the engine status in the title bar.';
const LOAD_FAILED = 'Aegis could not read your model settings. Restart the engine and try again.';

/** The core answered, but not with the shape this build expects. */
const UNREADABLE = 'The engine answered with something Aegis could not read.';

async function call(request: CoreRequest): Promise<BridgeResult<CoreResponse>> {
  try {
    return await window.aegis.core.request(request);
  } catch {
    // The bridge resolves rather than rejects, so this is a renderer bug — but a
    // screen stuck on "loading" forever is worse than the failure it hides.
    return { ok: false, error: { code: 'failed', message: 'bridge threw' } };
  }
}

/** A 2xx body, or `null` with the message to show. */
type Outcome<T> =
  { readonly ok: true; readonly value: T } | { readonly ok: false; readonly text: string };

async function fetchJson<T>(
  request: CoreRequest,
  parse: (body: unknown) => T | null,
  fallback: string,
): Promise<Outcome<T>> {
  const result = await call(request);
  if (!result.ok) {
    return { ok: false, text: result.error.code === 'unavailable' ? UNREACHABLE : fallback };
  }
  const { status, body } = result.value;
  if (status < 200 || status >= 300) {
    return { ok: false, text: coreDetail(status, body) ?? fallback };
  }
  const parsed = parse(body);
  return parsed === null ? { ok: false, text: UNREADABLE } : { ok: true, value: parsed };
}

/** Load everything the screen needs. Safe to call again; it replaces the draft. */
export async function loadModels(): Promise<void> {
  const store = useModelsStore.getState();
  store.beginLoad();
  const [catalog, settings, spend] = await Promise.all([
    fetchJson({ method: 'GET', path: '/models/catalog' }, parseCatalog, LOAD_FAILED),
    fetchJson({ method: 'GET', path: '/settings' }, parseSettings, LOAD_FAILED),
    fetchJson({ method: 'GET', path: '/models/spend' }, parseSpend, LOAD_FAILED),
  ]);
  if (!catalog.ok || !settings.ok || !spend.ok) {
    const first = [catalog, settings, spend].find((part) => !part.ok);
    useModelsStore.getState().failed(first !== undefined && !first.ok ? first.text : LOAD_FAILED);
    return;
  }
  useModelsStore
    .getState()
    .loaded({ catalog: catalog.value, settings: settings.value, spend: spend.value });
}

/** Write the draft. The core validates it and answers with what it stored. */
export async function saveSettings(draft: ModelSettings): Promise<void> {
  const store = useModelsStore.getState();
  store.beginSave();
  const outcome = await fetchJson(
    { method: 'PUT', path: '/settings', body: { models: draft } },
    parseSettings,
    'Aegis could not save that. Check the engine status in the title bar.',
  );
  useModelsStore
    .getState()
    .settled(
      outcome.ok ? outcome.value : null,
      outcome.ok ? { tone: 'ok', text: 'Saved.' } : { tone: 'error', text: outcome.text },
    );
}

/** The *Test* button: a real call to the provider, timed by the core. */
export async function testProvider(provider: ProviderId): Promise<void> {
  useModelsStore.getState().beginTest(provider);
  const outcome = await fetchJson(
    { method: 'POST', path: '/models/validate', body: { provider_id: provider } },
    parseValidate,
    UNREACHABLE,
  );
  useModelsStore.getState().tested(provider, outcome.ok ? outcome.value : null);
}

/**
 * Save a key, then reload the catalogue so the screen shows the masked form.
 *
 * The reload is what clears the field: the key the user typed is replaced by the
 * only representation of it that is allowed to exist here.
 */
export async function saveKey(provider: ProviderId, key: string): Promise<void> {
  await writeKey(
    provider,
    { method: 'PUT', path: `/models/keys/${provider}`, body: { key } },
    {
      tone: 'ok',
      text: 'Key saved.',
    },
  );
}

/** Remove a key. Removing one that is not there is not an error. */
export async function removeKey(provider: ProviderId): Promise<void> {
  await writeKey(
    provider,
    { method: 'DELETE', path: `/models/keys/${provider}` },
    {
      tone: 'ok',
      text: 'Key removed.',
    },
  );
}

async function writeKey(
  provider: ProviderId,
  request: CoreRequest,
  success: { tone: 'ok'; text: string },
): Promise<void> {
  useModelsStore.getState().beginKeySave(provider);
  const written = await fetchJson(
    request,
    (body) => (body === null || typeof body === 'object' ? {} : null),
    'Aegis could not save that key. Check the engine status in the title bar.',
  );
  if (!written.ok) {
    useModelsStore.getState().keySettled(provider, null, { tone: 'error', text: written.text });
    return;
  }
  const catalog = await fetchJson(
    { method: 'GET', path: '/models/catalog' },
    parseCatalog,
    LOAD_FAILED,
  );
  useModelsStore.getState().keySettled(
    provider,
    catalog.ok ? catalog.value : null,
    catalog.ok
      ? success
      : {
          tone: 'error',
          text: catalog.text,
        },
  );
}
