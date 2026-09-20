import type { ReactElement } from 'react';
import { Plus, TriangleAlert, X } from 'lucide-react';
import type {
  ModelCatalog,
  ModelChoiceSpec,
  ProviderCatalog,
  ProviderId,
  RoleRouteSpec,
} from '@aegis/shared';
import { Button } from '@/components/ui/button';
import { MAX_FALLBACKS, type RoleDescription } from '@/lib/model-roles';
import { cn } from '@/lib/utils';

/**
 * One role — `Planner`, `Grounder`, `Utility` — and the chain that answers for
 * it (`UI.md § 8.4`, `UI.md § 12`).
 *
 * Provider dropdown, then model, then the params, then a fallback chain the user
 * builds link by link. The chain is `primary → secondary → local` in
 * `ARCHITECTURE.md § 5.3`'s words, and it is bounded here at the same four links
 * the core refuses to exceed, so the *Add* button disappears rather than the
 * save failing.
 *
 * A provider whose models this build cannot enumerate gets a **text field**
 * rather than an empty dropdown (`free_text_model`), because `openrouter` and a
 * custom gateway serve catalogues no build-time table can hold, and a dropdown
 * with nothing in it would make a working model unreachable.
 *
 * Pure and controlled: it renders `route` and calls `onChange`. Whether that
 * change has been saved is the screen's business, not the card's.
 */

export interface ModelRoleCardProps {
  readonly role: RoleDescription;
  /** `null` until the user maps this role — the state on first run. */
  readonly route: RoleRouteSpec | null;
  readonly catalog: ModelCatalog;
  readonly onChange: (route: RoleRouteSpec | null) => void;
  readonly disabled?: boolean;
}

function providerOf(catalog: ModelCatalog, id: ProviderId): ProviderCatalog | undefined {
  return catalog.providers.find((provider) => provider.provider_id === id);
}

/** The first model this provider can offer, so choosing one never leaves it blank. */
function firstModel(provider: ProviderCatalog | undefined): string {
  return provider?.models[0]?.id ?? '';
}

export function ModelRoleCard({
  role,
  route,
  catalog,
  onChange,
  disabled = false,
}: ModelRoleCardProps): ReactElement {
  const chain = route === null ? [] : [route.primary, ...route.fallbacks];

  function replace(index: number, choice: ModelChoiceSpec): void {
    const next = chain.map((link, at) => (at === index ? choice : link));
    onChange({ primary: next[0] ?? choice, fallbacks: next.slice(1) });
  }

  function removeAt(index: number): void {
    const next = chain.filter((_, at) => at !== index);
    const primary = next[0];
    onChange(primary === undefined ? null : { primary, fallbacks: next.slice(1) });
  }

  function add(): void {
    const provider = catalog.providers[0];
    const choice: ModelChoiceSpec = {
      provider_id: provider?.provider_id ?? 'openai',
      model: firstModel(provider),
      temperature: null,
      max_output_tokens: null,
    };
    onChange(
      route === null
        ? { primary: choice, fallbacks: [] }
        : { primary: route.primary, fallbacks: [...route.fallbacks, choice] },
    );
  }

  return (
    <section
      aria-labelledby={`role-${role.id}`}
      className="rounded-card border border-border bg-surface-1 p-4"
    >
      <h3 id={`role-${role.id}`} className="text-base font-medium text-text">
        {role.label}
      </h3>
      <p className="mt-1 text-sm text-text-dim">{role.description}</p>

      {chain.length === 0 ? (
        <p className="mt-3 text-sm text-text-dim">No model is mapped to {role.label} yet.</p>
      ) : (
        <ol className="mt-3 flex flex-col gap-3">
          {chain.map((choice, index) => (
            <li key={`${role.id}-${String(index)}`}>
              <ChainLink
                role={role.id}
                index={index}
                choice={choice}
                catalog={catalog}
                disabled={disabled}
                onChange={(next) => {
                  replace(index, next);
                }}
                onRemove={() => {
                  removeAt(index);
                }}
              />
            </li>
          ))}
        </ol>
      )}

      {chain.length < MAX_FALLBACKS + 1 && (
        <Button
          variant="ghost"
          size="sm"
          className="mt-3"
          disabled={disabled}
          onClick={add}
          aria-label={
            chain.length === 0
              ? `Choose a model for ${role.label}`
              : `Add a fallback for ${role.label}`
          }
        >
          <Plus aria-hidden />
          {chain.length === 0 ? 'Choose a model' : 'Add a fallback'}
        </Button>
      )}
    </section>
  );
}

const FIELD =
  'h-8 min-w-0 rounded-control border border-border bg-surface-2 px-2 text-base text-text disabled:opacity-50';

function ChainLink({
  role,
  index,
  choice,
  catalog,
  disabled,
  onChange,
  onRemove,
}: {
  role: string;
  index: number;
  choice: ModelChoiceSpec;
  catalog: ModelCatalog;
  disabled: boolean;
  onChange: (choice: ModelChoiceSpec) => void;
  onRemove: () => void;
}): ReactElement {
  const provider = providerOf(catalog, choice.provider_id);
  const position = index === 0 ? 'Primary' : `Fallback ${String(index)}`;
  const id = `${role}-${String(index)}`;
  // The catalogue and the saved settings can disagree: a document written by an
  // older build, a model the provider has retired, a gateway that is no longer
  // configured. A `<select>` whose value is not among its options renders blank
  // and silently loses the setting, so both cases fall back to a text field and
  // say what is wrong instead.
  const unknown = unknownIn(provider, choice.model);
  const useTextField =
    provider === undefined ||
    provider.free_text_model ||
    provider.models.length === 0 ||
    unknown !== null;

  return (
    <div className="rounded-control border border-border bg-surface-2/40 p-3">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm font-medium text-text-dim">{position}</span>
        <Button
          variant="ghost"
          size="icon"
          disabled={disabled}
          aria-label={`Remove ${position.toLowerCase()} from ${role}`}
          onClick={onRemove}
        >
          <X aria-hidden />
        </Button>
      </div>

      {unknown !== null && (
        <p className="mt-2 flex items-start gap-1 text-sm text-caution" role="status">
          <TriangleAlert className="mt-0.5 size-4 shrink-0" aria-hidden />
          {unknown}
        </p>
      )}

      <div className="mt-2 grid gap-2 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm text-text-dim">
          Provider
          <select
            id={`${id}-provider`}
            className={FIELD}
            disabled={disabled}
            value={choice.provider_id}
            onChange={(event) => {
              const next = event.target.value as ProviderId;
              onChange({
                ...choice,
                provider_id: next,
                model: firstModel(providerOf(catalog, next)),
              });
            }}
          >
            {catalog.providers.map((entry) => (
              <option key={entry.provider_id} value={entry.provider_id}>
                {entry.label}
              </option>
            ))}
          </select>
        </label>

        <label className="flex flex-col gap-1 text-sm text-text-dim">
          Model
          {!useTextField && provider !== undefined ? (
            <select
              id={`${id}-model`}
              className={FIELD}
              disabled={disabled}
              value={choice.model}
              onChange={(event) => {
                onChange({ ...choice, model: event.target.value });
              }}
            >
              {provider.models.map((model) => (
                <option key={model.id} value={model.id}>
                  {model.id}
                </option>
              ))}
            </select>
          ) : (
            <input
              id={`${id}-model`}
              type="text"
              list={`${id}-models`}
              className={cn(FIELD, 'placeholder:text-text-dim')}
              disabled={disabled}
              placeholder="Model id"
              value={choice.model}
              onChange={(event) => {
                onChange({ ...choice, model: event.target.value });
              }}
            />
          )}
          {provider !== undefined && useTextField && provider.models.length > 0 && (
            <datalist id={`${id}-models`}>
              {provider.models.map((model) => (
                <option key={model.id} value={model.id} />
              ))}
            </datalist>
          )}
        </label>

        <label className="flex flex-col gap-1 text-sm text-text-dim">
          Temperature
          <input
            id={`${id}-temperature`}
            type="number"
            min={0}
            max={2}
            step={0.1}
            className={FIELD}
            disabled={disabled}
            placeholder="Provider default"
            value={choice.temperature ?? ''}
            onChange={(event) => {
              onChange({ ...choice, temperature: numberOrNull(event.target.value) });
            }}
          />
        </label>

        <label className="flex flex-col gap-1 text-sm text-text-dim">
          Max output tokens
          <input
            id={`${id}-max-output`}
            type="number"
            min={1}
            step={1}
            className={FIELD}
            disabled={disabled}
            placeholder="Provider default"
            value={choice.max_output_tokens ?? ''}
            onChange={(event) => {
              onChange({ ...choice, max_output_tokens: numberOrNull(event.target.value) });
            }}
          />
        </label>
      </div>
    </div>
  );
}

/**
 * Why this link cannot be shown as a choice from the catalogue, or `null`.
 *
 * Both cases are settings that no longer match what this build can offer, and
 * both have to be *visible*: a role silently pointing at a model that is not
 * there is a task that fails at its first step with nothing on screen to explain
 * it. The value is kept either way — it is what the user chose, and the core is
 * what decides whether it still works.
 */
function unknownIn(provider: ProviderCatalog | undefined, model: string): string | null {
  if (provider === undefined) {
    return 'Aegis does not recognise the provider saved here. Choose one from the list.';
  }
  if (provider.free_text_model || provider.models.length === 0 || model === '') return null;
  return provider.models.some((entry) => entry.id === model)
    ? null
    : `${provider.label} no longer lists this model. Check the id, or pick another.`;
}

/** An empty field means *no opinion*, which is `null` — never `0`. */
function numberOrNull(raw: string): number | null {
  if (raw.trim() === '') return null;
  const value = Number(raw);
  return Number.isFinite(value) ? value : null;
}
