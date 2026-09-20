import type { ReactElement } from 'react';
import { Check, Key, ShieldCheck, Trash2, X } from 'lucide-react';
import type { ProviderCatalog, ValidateResponse } from '@aegis/shared';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/**
 * One provider: its key, its address, and the *Test* button (`UI.md § 8.4`,
 * `UI.md § 12`).
 *
 * Three things it is careful about.
 *
 * **It never displays a key.** The field is for typing a new one; what is saved
 * shows as `masked_key`, which is the only representation of a stored key that
 * exists anywhere in the renderer (`ARCHITECTURE.md § 5.3`). The input is
 * `type="password"` with autocomplete off so neither the browser nor a
 * screenshot keeps a copy.
 *
 * **The local card says what it means.** `UI.md § 8.4` asks for a *Nothing
 * leaves your PC* badge on Ollama, and it is a badge with a word and an icon,
 * never a colour on its own (`REVIEW.md § 3`).
 *
 * **A test result is shown as it came back.** The core writes that sentence for
 * the user and never quotes a key or a response body, so it passes through.
 */

export interface ProviderCardProps {
  readonly provider: ProviderCatalog;
  /**
   * The address as the **draft** holds it, not as the catalogue reported it: the
   * field is part of the unsaved settings, so typing in it must not be thrown
   * away by the next catalogue reload.
   */
  readonly baseUrl: string;
  /** What is typed in the key field right now, never a saved key. */
  readonly keyDraft: string;
  readonly result: ValidateResponse | undefined;
  readonly testing: boolean;
  readonly savingKey: boolean;
  readonly disabled?: boolean;
  readonly onKeyDraft: (key: string) => void;
  readonly onSaveKey: () => void;
  readonly onRemoveKey: () => void;
  readonly onTest: () => void;
  readonly onBaseUrl: (baseUrl: string) => void;
}

const FIELD =
  'h-8 min-w-0 flex-1 rounded-control border border-border bg-surface-2 px-2 text-base text-text placeholder:text-text-dim disabled:opacity-50';

export function ProviderCard({
  provider,
  baseUrl,
  keyDraft,
  result,
  testing,
  savingKey,
  disabled = false,
  onKeyDraft,
  onSaveKey,
  onRemoveKey,
  onTest,
  onBaseUrl,
}: ProviderCardProps): ReactElement {
  const id = provider.provider_id;
  const busy = disabled || testing || savingKey;

  return (
    <section
      aria-labelledby={`provider-${id}`}
      className="rounded-card border border-border bg-surface-1 p-4"
    >
      <div className="flex flex-wrap items-center gap-2">
        <h3 id={`provider-${id}`} className="text-base font-medium text-text">
          {provider.label}
        </h3>
        {provider.local && (
          <span className="inline-flex items-center gap-1 rounded-pill border border-safe px-2 py-0.5 text-xs text-safe">
            <ShieldCheck className="size-3" aria-hidden />
            Nothing leaves your PC
          </span>
        )}
        {provider.has_key && provider.masked_key !== null && (
          <span className="inline-flex items-center gap-1 rounded-pill border border-border px-2 py-0.5 text-xs text-text-dim">
            <Key className="size-3" aria-hidden />
            {provider.masked_key}
          </span>
        )}
      </div>

      {provider.detail !== null && <p className="mt-2 text-sm text-text-dim">{provider.detail}</p>}

      {provider.editable_base_url && (
        <label className="mt-3 flex flex-col gap-1 text-sm text-text-dim">
          Address
          <input
            id={`${id}-base-url`}
            type="text"
            inputMode="url"
            className={FIELD}
            disabled={busy}
            placeholder="https://gateway.example/v1"
            value={baseUrl}
            onChange={(event) => {
              onBaseUrl(event.target.value);
            }}
          />
        </label>
      )}

      {provider.requires_key ? (
        <div className="mt-3 flex flex-wrap items-end gap-2">
          <label className="flex min-w-50 flex-1 flex-col gap-1 text-sm text-text-dim">
            {provider.has_key ? 'Replace key' : 'API key'}
            <input
              id={`${id}-key`}
              type="password"
              autoComplete="off"
              spellCheck={false}
              className={FIELD}
              disabled={busy}
              placeholder="Paste your key"
              value={keyDraft}
              onChange={(event) => {
                onKeyDraft(event.target.value);
              }}
            />
          </label>
          <Button variant="secondary" disabled={busy || keyDraft.trim() === ''} onClick={onSaveKey}>
            {savingKey ? 'Saving…' : 'Save key'}
          </Button>
          {provider.has_key && (
            <Button
              variant="ghost"
              disabled={busy}
              onClick={onRemoveKey}
              aria-label={`Remove the saved key for ${provider.label}`}
            >
              <Trash2 aria-hidden />
              Remove
            </Button>
          )}
        </div>
      ) : (
        <p className="mt-3 text-sm text-text-dim">
          This provider needs no key — it runs on this machine.
        </p>
      )}

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <Button variant="secondary" disabled={busy} onClick={onTest}>
          {testing ? 'Testing…' : 'Test'}
        </Button>
        <p aria-live="polite" className="min-h-5 flex-1 text-sm">
          {result !== undefined && (
            <span
              className={cn(
                'inline-flex items-center gap-1',
                result.valid ? 'text-safe' : 'text-danger',
              )}
            >
              {result.valid ? (
                <Check className="size-4" aria-hidden />
              ) : (
                <X className="size-4" aria-hidden />
              )}
              {result.detail} ({result.latency_ms} ms)
            </span>
          )}
        </p>
      </div>
    </section>
  );
}
