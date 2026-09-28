import { useEffect, useRef, useState, type ReactElement } from 'react';
import { Check, ChevronDown, FolderCog } from 'lucide-react';
import { canStop, hudState } from '@/lib/hud-state';
import { selectScope } from '@/lib/scopes-client';
import { cn } from '@/lib/utils';
import { selectedScope, useScopesStore } from '@/stores/scopes';
import { useStreamStore } from '@/stores/stream';

/**
 * The titlebar's scope selector (`UI.md § 4.1`, § 12's `ScopePicker`; P3-17): which
 * scope the next task runs in. It lists the scopes and links to *Manage scopes…*; it
 * never adds a folder itself — only the Manage-scopes list can, through the dialog.
 *
 * Changing scope while a task is going is refused, with the reason on hover, because the
 * Guardian decided that task's calls against the scope it started with.
 */
export function ScopePicker({ onManage }: { onManage: () => void }): ReactElement {
  const status = useScopesStore((state) => state.status);
  const scopes = useScopesStore((state) => state.scopes);
  const current = useScopesStore(selectedScope);
  const taskGoing = canStop(hudState(useStreamStore()));
  const [open, setOpen] = useState(false);
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return undefined;
    const close = (event: MouseEvent | KeyboardEvent): void => {
      if (
        event instanceof KeyboardEvent
          ? event.key === 'Escape'
          : !root.current?.contains(event.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('mousedown', close);
      document.removeEventListener('keydown', close);
    };
  }, [open]);

  const label =
    status === 'loading' && scopes.length === 0
      ? 'Scope: …'
      : status === 'error'
        ? 'Scope: unavailable'
        : `Scope: ${current?.name ?? 'none'}`;

  function choose(id: number | null): void {
    selectScope(id);
    setOpen(false);
  }

  const item =
    'flex w-full items-center gap-2 rounded-control px-2 py-1.5 text-left text-sm text-text hover:bg-surface-2';

  return (
    <div ref={root} className="app-no-drag relative">
      <button
        type="button"
        aria-haspopup="menu"
        aria-expanded={open}
        disabled={taskGoing}
        title={
          taskGoing
            ? 'You can’t change the scope while a task is running.'
            : 'Which folders Aegis may work in'
        }
        onClick={() => {
          setOpen((value) => !value);
        }}
        className={cn(
          'flex max-w-56 items-center gap-1 rounded-pill border border-border px-2.5 py-1 text-sm',
          status === 'error' ? 'text-danger' : 'text-text-dim',
          'hover:border-text-dim disabled:opacity-50',
        )}
      >
        <span className="truncate">{label}</span>
        <ChevronDown className="size-3.5 shrink-0" aria-hidden />
      </button>

      {open && (
        <div
          role="menu"
          aria-label="Scopes"
          className="absolute right-0 top-full z-20 mt-1 w-64 rounded-card border border-border bg-surface-1 p-1 shadow-lg"
        >
          {scopes.length === 0 ? (
            <p className="px-2 py-1.5 text-sm text-text-dim">No scopes yet.</p>
          ) : (
            <>
              <button
                type="button"
                role="menuitemradio"
                aria-checked={current === null}
                className={item}
                onClick={() => {
                  choose(null);
                }}
              >
                <Check
                  className={cn('size-3.5', current === null ? 'opacity-100' : 'opacity-0')}
                  aria-hidden
                />
                None
              </button>
              {scopes.map((scope) => (
                <button
                  key={scope.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={current?.id === scope.id}
                  className={item}
                  onClick={() => {
                    choose(scope.id);
                  }}
                >
                  <Check
                    className={cn(
                      'size-3.5 shrink-0',
                      current?.id === scope.id ? 'opacity-100' : 'opacity-0',
                    )}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1 truncate">{scope.name}</span>
                  <span className="shrink-0 text-xs text-text-dim">
                    {scope.folders.length === 1
                      ? '1 folder'
                      : `${String(scope.folders.length)} folders`}
                  </span>
                </button>
              ))}
            </>
          )}
          <div className="my-1 border-t border-border" />
          <button
            type="button"
            role="menuitem"
            className={item}
            onClick={() => {
              setOpen(false);
              onManage();
            }}
          >
            <FolderCog className="size-3.5" aria-hidden />
            Manage scopes…
          </button>
        </div>
      )}
    </div>
  );
}
