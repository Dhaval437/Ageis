import { useEffect, type ReactElement } from 'react';
import { Folder, ListChecks, RotateCw } from 'lucide-react';
import type { AllowRuleInfo } from '@aegis/shared';
import { Button } from '@/components/ui/button';
import { describeRule, formatCreated } from '@/lib/rules-api';
import { loadRules, revokeRule } from '@/lib/rules-client';
import { cn } from '@/lib/utils';
import { useRulesStore } from '@/stores/rules';
import { useStreamStore } from '@/stores/stream';

/**
 * *Always allow* (`UI.md § 8.3`, P3-18): every rule the person made with *Allow always ▾*
 * in the approval dialog, each with what it allows, where, when it was made, and
 * **Revoke** — which is what makes an *Allow always* something a person can take back
 * (`REVIEW.md § 5`).
 *
 * Revoking only removes a permission, so it is one click: the worst a wrong click can
 * do is make Aegis ask again. The list is read each time the screen opens and each time
 * the engine comes (back) up, so a rule made since the last visit is always shown.
 */
export function AlwaysAllowRules(): ReactElement {
  const status = useRulesStore((state) => state.status);
  const rules = useRulesStore((state) => state.rules);
  const loadError = useRulesStore((state) => state.loadError);
  const revoking = useRulesStore((state) => state.revoking);
  const notice = useRulesStore((state) => state.notice);
  const connection = useStreamStore((state) => state.connection);

  useEffect(() => {
    if (connection === 'live') void loadRules();
  }, [connection]);

  return (
    <section aria-labelledby="always-allow-heading" className="flex flex-col gap-4 px-6 pt-6">
      <header>
        <h2 id="always-allow-heading" className="text-lg font-medium text-text">
          Always allow
        </h2>
        <p className="text-base text-text-dim">
          Actions you told Aegis it may take without asking you first. They apply only when autonomy
          is Trusted, and never to dangerous actions. Revoke one and Aegis asks again.
        </p>
      </header>

      <p
        aria-live="polite"
        className={cn(
          'min-h-5 text-sm',
          notice?.tone === 'error' ? 'text-danger' : 'text-text-dim',
        )}
      >
        {notice?.text ?? ''}
      </p>

      {status === 'error' ? (
        <div
          role="alert"
          className="flex items-center gap-3 rounded-card border border-danger bg-surface-2 p-3"
        >
          <p className="flex-1 text-base text-text">{loadError}</p>
          <Button
            size="sm"
            onClick={() => {
              void loadRules();
            }}
          >
            <RotateCw aria-hidden />
            Try again
          </Button>
        </div>
      ) : (status === 'loading' || status === 'idle') && rules.length === 0 ? (
        <div aria-busy="true" aria-label="Loading rules" className="flex flex-col gap-3">
          <div className="h-16 animate-pulse rounded-card bg-surface-2" />
          <div className="h-16 animate-pulse rounded-card bg-surface-2" />
        </div>
      ) : rules.length === 0 ? (
        <p className="rounded-card border border-border bg-surface-1 p-4 text-base text-text-dim">
          No rules yet. Aegis asks you before every action that needs your approval.
        </p>
      ) : (
        <ul aria-label="Always-allow rules" className="flex flex-col gap-2">
          {rules.map((rule) => (
            <li key={rule.id}>
              <RuleRow rule={rule} busy={revoking === rule.id} locked={revoking !== null} />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

/** `UI.md § 12`'s `RuleRow`: what, where, when made, and Revoke. */
function RuleRow({
  rule,
  busy,
  locked,
}: {
  rule: AllowRuleInfo;
  busy: boolean;
  locked: boolean;
}): ReactElement {
  return (
    <article
      aria-label={`${rule.tool}: ${describeRule(rule)}`}
      aria-busy={busy}
      className="flex items-start gap-3 rounded-card border border-border bg-surface-1 p-3"
    >
      <div className="flex min-w-0 flex-1 flex-col gap-1">
        <p className="truncate font-mono text-sm text-text" title={rule.tool}>
          {rule.tool}
        </p>
        <p className="text-sm text-text-dim">{describeRule(rule)}</p>
        {rule.kind === 'tool_in_folder' && rule.folder !== null && (
          <p className="flex min-w-0 items-center gap-1.5 text-sm text-text-dim">
            <Folder className="size-3.5 shrink-0" aria-hidden />
            <span className="truncate font-mono text-xs" title={rule.folder}>
              {rule.folder}
            </span>
          </p>
        )}
        {rule.kind === 'tool_for_task' && rule.task_id !== null && (
          <p className="flex min-w-0 items-center gap-1.5 text-sm text-text-dim">
            <ListChecks className="size-3.5 shrink-0" aria-hidden />
            <span className="truncate">
              Task <span className="font-mono text-xs">{rule.task_id}</span>
            </span>
          </p>
        )}
        <p className="text-xs text-text-dim">Made {formatCreated(rule.created_at)}</p>
      </div>
      <Button
        size="sm"
        disabled={locked}
        aria-label={`Revoke the rule for ${rule.tool}`}
        onClick={() => {
          void revokeRule(rule);
        }}
      >
        {busy ? 'Revoking…' : 'Revoke'}
      </Button>
    </article>
  );
}
