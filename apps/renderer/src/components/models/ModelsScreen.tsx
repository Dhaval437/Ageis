import { useEffect, useMemo, type ReactElement } from 'react';
import type { BudgetLimitsSpec, ModelSettings, ProviderId } from '@aegis/shared';
import { ModelRoleCard } from '@/components/models/ModelRoleCard';
import { ProviderCard } from '@/components/models/ProviderCard';
import { SpendMeter } from '@/components/models/SpendMeter';
import { Button } from '@/components/ui/button';
import { ROLES, unmappedRoles, withRoute } from '@/lib/model-roles';
import { loadModels, removeKey, saveKey, saveSettings, testProvider } from '@/lib/models-client';
import { latestDaySpend } from '@/lib/spend';
import { cn } from '@/lib/utils';
import { hasUnsavedChanges, useModelsStore } from '@/stores/models';
import { useStreamStore } from '@/stores/stream';

/**
 * The Models screen (`UI.md § 8.4`): one card per role, the providers below,
 * budget ceilings, and a live spend bar.
 *
 * It loads once, when it is first shown, and everything after that is pushed:
 * the spend meter prefers the last `cost.updated` on the stream over the value
 * it was loaded with, so the bar moves while a task runs without this screen
 * polling for it (invariant 15).
 *
 * Edits go into a **draft**. Nothing is sent until *Save changes*, and the core
 * is what validates it — a chain that repeats a model, or a gateway address a
 * key may not be sent to, comes back as one sentence the user can act on
 * (`ARCHITECTURE.md § 9.1`). The screen does not duplicate those rules; it only
 * refuses to offer a fifth link in a chain the core caps at four.
 */

export function ModelsScreen(): ReactElement {
  const status = useModelsStore((state) => state.status);
  const load = useModelsStore((state) => state.beginLoad);

  useEffect(() => {
    // `idle` means nothing has asked yet; a remount after a failure keeps the
    // error on screen with its Try again button rather than looping on it.
    if (useModelsStore.getState().status === 'idle') {
      void loadModels();
    }
  }, [load]);

  return (
    <div className="h-full w-full overflow-y-auto">
      <div className="mx-auto flex w-full max-w-240 flex-col gap-4 p-6">
        <header>
          <h1 className="text-lg font-medium text-text">Models</h1>
          <p className="mt-1 text-base text-text-dim">
            Aegis uses three models. Map each one, add your keys, and set what you are willing to
            spend.
          </p>
        </header>
        {status === 'loading' || status === 'idle' ? <Skeleton /> : null}
        {status === 'error' ? <LoadFailed /> : null}
        {status === 'ready' ? <Loaded /> : null}
      </div>
    </div>
  );
}

/** `UI.md § 9`: loading is skeletons for lists, never a bare spinner. */
function Skeleton(): ReactElement {
  return (
    <div aria-busy="true" aria-label="Loading your model settings" className="flex flex-col gap-4">
      {[0, 1, 2].map((row) => (
        <div
          key={row}
          className="h-28 animate-pulse rounded-card border border-border bg-surface-1"
        />
      ))}
    </div>
  );
}

function LoadFailed(): ReactElement {
  const error = useModelsStore((state) => state.error);
  return (
    <section
      aria-labelledby="models-error"
      className="rounded-card border border-danger bg-surface-1 p-4"
    >
      <h2 id="models-error" className="text-base font-medium text-text">
        Aegis could not load your model settings
      </h2>
      <p className="mt-2 text-base text-text-dim">{error}</p>
      <Button
        variant="secondary"
        className="mt-3"
        onClick={() => {
          void loadModels();
        }}
      >
        Try again
      </Button>
    </section>
  );
}

function Loaded(): ReactElement {
  const catalog = useModelsStore((state) => state.catalog);
  const draft = useModelsStore((state) => state.draft);
  const spend = useModelsStore((state) => state.spend);
  const saving = useModelsStore((state) => state.saving);
  const notice = useModelsStore((state) => state.notice);
  const testing = useModelsStore((state) => state.testing);
  const savingKey = useModelsStore((state) => state.savingKey);
  const results = useModelsStore((state) => state.results);
  const keyDrafts = useModelsStore((state) => state.keyDrafts);
  const edit = useModelsStore((state) => state.edit);
  const revert = useModelsStore((state) => state.revert);
  const typeKey = useModelsStore((state) => state.typeKey);
  const dirty = useModelsStore(hasUnsavedChanges);
  // The meter's live value: the last `cost.updated` if the stream has carried
  // one, otherwise what the screen loaded with. The selector returns the events
  // array itself — a selector that derived the totals would build a new object
  // on every store notification, and zustand compares by reference, so the
  // component would re-render forever.
  const events = useStreamStore((state) => state.events);
  const streamed = useMemo(() => latestDaySpend(events), [events]);

  if (catalog === null || draft === null) return <Skeleton />;

  const missing = unmappedRoles(draft);

  return (
    <>
      {missing.length > 0 && (
        <p className="rounded-card border border-caution bg-surface-1 p-3 text-base text-caution">
          {missing.map((role) => role.label).join(' and ')} {missing.length === 1 ? 'has' : 'have'}{' '}
          no model yet. Aegis cannot run a task until all three do.
        </p>
      )}

      <section aria-labelledby="models-roles" className="flex flex-col gap-3">
        <h2 id="models-roles" className="text-md font-medium text-text">
          Roles
        </h2>
        {ROLES.map((role) => (
          <ModelRoleCard
            key={role.id}
            role={role}
            route={draft[role.id]}
            catalog={catalog}
            disabled={saving}
            onChange={(route) => {
              edit((current) => withRoute(current, role.id, route));
            }}
          />
        ))}
      </section>

      <section aria-labelledby="models-providers" className="flex flex-col gap-3">
        <h2 id="models-providers" className="text-md font-medium text-text">
          Providers
        </h2>
        {catalog.providers.map((provider) => (
          <ProviderCard
            key={provider.provider_id}
            provider={provider}
            baseUrl={baseUrlOf(draft, provider.provider_id) ?? ''}
            keyDraft={keyDrafts[provider.provider_id] ?? ''}
            result={results[provider.provider_id]}
            testing={testing === provider.provider_id}
            savingKey={savingKey === provider.provider_id}
            disabled={saving}
            onKeyDraft={(key) => {
              typeKey(provider.provider_id, key);
            }}
            onSaveKey={() => {
              void saveKey(provider.provider_id, keyDrafts[provider.provider_id] ?? '');
            }}
            onRemoveKey={() => {
              void removeKey(provider.provider_id);
            }}
            onTest={() => {
              void testProvider(provider.provider_id);
            }}
            onBaseUrl={(baseUrl) => {
              edit((current) => withBaseUrl(current, provider.provider_id, baseUrl));
            }}
          />
        ))}
      </section>

      <section aria-labelledby="models-budget" className="flex flex-col gap-3">
        <h2 id="models-budget" className="text-md font-medium text-text">
          Budget
        </h2>
        <SpendMeter day={streamed ?? spend?.day ?? null} limits={draft.limits} />
        <BudgetFields
          limits={draft.limits}
          disabled={saving}
          onChange={(limits) => {
            edit((current) => ({ ...current, limits }));
          }}
        />
      </section>

      <div className="sticky bottom-0 flex flex-wrap items-center gap-3 border-t border-border bg-bg py-3">
        <Button
          variant="accent"
          disabled={saving || !dirty}
          onClick={() => {
            void saveSettings(draft);
          }}
        >
          {saving ? 'Saving…' : 'Save changes'}
        </Button>
        <Button variant="ghost" disabled={saving || !dirty} onClick={revert}>
          Discard
        </Button>
        <p aria-live="polite" className="min-h-5 flex-1 text-sm">
          {notice !== null && (
            <span className={cn(notice.tone === 'error' ? 'text-danger' : 'text-text-dim')}>
              {notice.text}
            </span>
          )}
          {notice === null && dirty && <span className="text-text-dim">Unsaved changes.</span>}
        </p>
      </div>
    </>
  );
}

const CEILINGS: readonly {
  readonly field: keyof BudgetLimitsSpec;
  readonly label: string;
  readonly hint: string;
  readonly money: boolean;
}[] = [
  { field: 'task_cents', label: 'Per task', hint: 'US dollars', money: true },
  { field: 'day_cents', label: 'Per day', hint: 'US dollars', money: true },
  { field: 'task_tokens', label: 'Tokens per task', hint: 'Backstop', money: false },
  { field: 'day_tokens', label: 'Tokens per day', hint: 'Backstop', money: false },
];

/**
 * The four ceilings (`models/budget.py`). Money is typed in dollars and stored
 * in cents, because a user thinks in dollars and the ledger counts fractions of
 * a cent. An empty field is **no ceiling**, which is what `null` means — and the
 * token ceilings are the only guard on a provider whose price Aegis does not
 * know, so clearing one is a real decision and the copy says so.
 */
function BudgetFields({
  limits,
  disabled,
  onChange,
}: {
  limits: BudgetLimitsSpec;
  disabled: boolean;
  onChange: (limits: BudgetLimitsSpec) => void;
}): ReactElement {
  return (
    <div className="rounded-card border border-border bg-surface-1 p-4">
      <div className="grid gap-3 sm:grid-cols-2">
        {CEILINGS.map(({ field, label, hint, money }) => {
          const stored = limits[field];
          const shown = stored === null ? '' : String(money ? stored / 100 : stored);
          return (
            <label key={field} className="flex flex-col gap-1 text-sm text-text-dim">
              {label} <span className="text-xs">({hint})</span>
              <input
                id={`limit-${field}`}
                type="number"
                min={0}
                step={money ? 0.01 : 1000}
                disabled={disabled}
                placeholder="No limit"
                className="h-8 min-w-0 rounded-control border border-border bg-surface-2 px-2 text-base text-text placeholder:text-text-dim disabled:opacity-50"
                value={shown}
                onChange={(event) => {
                  onChange({ ...limits, [field]: toLimit(event.target.value, money) });
                }}
              />
            </label>
          );
        })}
      </div>
      <p className="mt-3 text-sm text-text-dim">
        A task pauses when it reaches a ceiling; it never carries on quietly. Leave a field empty
        for no limit — but the token ceilings are the only guard on a provider whose prices Aegis
        does not know.
      </p>
    </div>
  );
}

/** An empty field is no ceiling. A zero is not a ceiling the core will accept, so it is one too. */
function toLimit(raw: string, money: boolean): number | null {
  if (raw.trim() === '') return null;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= 0) return null;
  return money ? Math.round(value * 100 * 1e4) / 1e4 : Math.round(value);
}

function baseUrlOf(settings: ModelSettings, provider: ProviderId): string | null {
  if (provider === 'custom') return settings.custom_base_url;
  if (provider === 'ollama') return settings.ollama_base_url;
  return null;
}

function withBaseUrl(
  settings: ModelSettings,
  provider: ProviderId,
  baseUrl: string,
): ModelSettings {
  const value = baseUrl.trim() === '' ? null : baseUrl;
  if (provider === 'custom') return { ...settings, custom_base_url: value };
  if (provider === 'ollama') return { ...settings, ollama_base_url: value };
  return settings;
}
