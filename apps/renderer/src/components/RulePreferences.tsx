import { useState, type FormEvent, type ReactElement } from 'react';
import { LockKeyhole, RotateCw } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { addAskRule, loadRules, removeAskRule } from '@/lib/rules-client';
import { useRulesStore } from '@/stores/rules';

export function RulePreferences(): ReactElement {
  const [tool, setTool] = useState('');
  const status = useRulesStore((state) => state.status);
  const alwaysAsk = useRulesStore((state) => state.alwaysAsk);
  const never = useRulesStore((state) => state.never);
  const changing = useRulesStore((state) => state.changingAsk);

  function submit(event: FormEvent): void {
    event.preventDefault();
    const trimmed = tool.trim();
    if (trimmed === '') return;
    void addAskRule(trimmed).then(() => setTool(''));
  }

  if (status === 'error') {
    return (
      <section aria-labelledby="rule-preferences-heading" className="flex flex-col gap-3 px-6 pt-6">
        <h2 id="rule-preferences-heading" className="text-lg font-medium text-text">
          Always ask and Never
        </h2>
        <div role="alert" className="flex items-center gap-3 rounded-card border border-danger p-3">
          <p className="flex-1 text-base text-text">Aegis could not read these rules.</p>
          <Button size="sm" onClick={() => void loadRules()}>
            <RotateCw aria-hidden /> Try again
          </Button>
        </div>
      </section>
    );
  }

  return (
    <div className="flex flex-col">
      <section aria-labelledby="always-ask-heading" className="flex flex-col gap-4 px-6 pt-6">
        <header>
          <h2 id="always-ask-heading" className="text-lg font-medium text-text">
            Always ask
          </h2>
          <p className="text-base text-text-dim">
            These tools always need your approval, even when your autonomy setting would allow them.
          </p>
        </header>
        <form className="flex items-end gap-2" onSubmit={submit}>
          <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm text-text-dim">
            Tool name
            <input
              value={tool}
              pattern="[a-z][a-z0-9_.-]*"
              maxLength={128}
              placeholder="fs.write"
              onChange={(event) => setTool(event.target.value)}
              className="h-9 rounded-control border border-border bg-surface-1 px-3 font-mono text-sm text-text outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
            />
          </label>
          <Button size="sm" type="submit" disabled={tool.trim() === '' || changing !== null}>
            Always ask
          </Button>
        </form>
        {status === 'loading' || status === 'idle' ? (
          <div
            aria-busy="true"
            aria-label="Loading always-ask rules"
            className="h-14 animate-pulse rounded-card bg-surface-2"
          />
        ) : alwaysAsk.length === 0 ? (
          <p className="rounded-card border border-border bg-surface-1 p-4 text-base text-text-dim">
            No tools are escalated. Your autonomy setting decides when Aegis asks.
          </p>
        ) : (
          <ul aria-label="Always-ask rules" className="flex flex-col gap-2">
            {alwaysAsk.map((rule) => (
              <li
                key={rule.tool}
                className="flex items-center gap-3 rounded-card border border-border bg-surface-1 p-3"
              >
                <span className="min-w-0 flex-1 truncate font-mono text-sm text-text">
                  {rule.tool}
                </span>
                <Button
                  size="sm"
                  disabled={changing !== null}
                  onClick={() => void removeAskRule(rule.tool)}
                >
                  {changing === rule.tool ? 'Changing…' : 'Use autonomy setting'}
                </Button>
              </li>
            ))}
          </ul>
        )}
      </section>

      <section aria-labelledby="never-heading" className="flex flex-col gap-4 px-6 py-6">
        <header>
          <h2 id="never-heading" className="flex items-center gap-2 text-lg font-medium text-text">
            <LockKeyhole className="size-4" aria-hidden /> Never
          </h2>
          <p className="text-base text-text-dim">
            These can't be enabled. They're built into Aegis.
          </p>
        </header>
        {status === 'loading' || status === 'idle' ? (
          <div
            aria-busy="true"
            aria-label="Loading built-in rules"
            className="h-24 animate-pulse rounded-card bg-surface-2"
          />
        ) : never.length === 0 ? (
          <p role="alert" className="rounded-card border border-danger p-4 text-base text-text">
            Aegis could not show its built-in rules. Restart the engine and try again.
          </p>
        ) : (
          <ul aria-label="Built-in never rules" className="grid gap-2 sm:grid-cols-2">
            {never.map((label, index) => (
              <li
                key={`${String(index)}-${label}`}
                className="flex gap-2 rounded-card border border-border bg-surface-1 p-3 text-sm text-text"
              >
                <LockKeyhole className="mt-0.5 size-3.5 shrink-0 text-danger" aria-label="Locked" />
                <span>{label}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
