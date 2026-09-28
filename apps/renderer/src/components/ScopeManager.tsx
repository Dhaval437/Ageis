import { useState, type ReactElement } from 'react';
import { FolderPlus, RotateCw, Trash2, X } from 'lucide-react';
import type { ScopeInfo } from '@aegis/shared';
import { Button } from '@/components/ui/button';
import { whatThisAllows } from '@/lib/scopes-api';
import {
  addScopeFolder,
  createScope,
  deleteScope,
  loadScopes,
  updateScope,
} from '@/lib/scopes-client';
import { cn } from '@/lib/utils';
import { useScopesStore } from '@/stores/scopes';

/**
 * Manage scopes (`UI.md § 8.3`, P3-17): named scopes, each a list of folders with a
 * plain-English "what this allows".
 *
 * **Every folder comes from the OS dialog.** *Choose folder…* and *Add folder…* open it
 * in MAIN, and there is no text field a folder could be typed or pasted into — so text
 * the agent read off a screen cannot become a folder it may work in. Removing a folder,
 * renaming and deleting only narrow, and say that no file is touched.
 *
 * Apps are listed once a scope has any; nothing adds them yet (`P5`).
 */

const MAX_NAME = 64;

export function ScopeManager(): ReactElement {
  const status = useScopesStore((state) => state.status);
  const scopes = useScopesStore((state) => state.scopes);
  const loadError = useScopesStore((state) => state.loadError);
  const busy = useScopesStore((state) => state.busy);
  const notice = useScopesStore((state) => state.notice);
  const [name, setName] = useState('');
  const trimmed = name.trim();

  return (
    <section
      aria-labelledby="scopes-heading"
      className="flex w-full flex-col gap-4 overflow-y-auto p-6"
    >
      <header>
        <h2 id="scopes-heading" className="text-lg font-medium text-text">
          Scopes
        </h2>
        <p className="text-base text-text-dim">
          A scope is the set of folders Aegis may work in for a task. You add every folder yourself,
          in the folder picker.
        </p>
      </header>

      <form
        className="flex items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (trimmed === '') return;
          void createScope(trimmed).then(() => {
            if (useScopesStore.getState().notice?.tone === 'info') setName('');
          });
        }}
      >
        <input
          type="text"
          value={name}
          maxLength={MAX_NAME}
          onChange={(event) => {
            setName(event.target.value);
          }}
          aria-label="New scope name"
          placeholder="New scope name, such as Work files"
          className="h-8 min-w-0 flex-1 rounded-control border border-border bg-surface-2 px-3 text-base text-text placeholder:text-text-dim"
        />
        <Button type="submit" variant="accent" disabled={trimmed === '' || busy !== null}>
          <FolderPlus aria-hidden />
          {busy === 'create' ? 'Choosing…' : 'Choose folder…'}
        </Button>
      </form>

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
              void loadScopes();
            }}
          >
            <RotateCw aria-hidden />
            Try again
          </Button>
        </div>
      ) : status === 'loading' && scopes.length === 0 ? (
        <div aria-busy="true" aria-label="Loading scopes" className="flex flex-col gap-3">
          <div className="h-24 animate-pulse rounded-card bg-surface-2" />
          <div className="h-24 animate-pulse rounded-card bg-surface-2" />
        </div>
      ) : scopes.length === 0 ? (
        <p className="rounded-card border border-border bg-surface-1 p-4 text-base text-text-dim">
          No scopes yet. Until you make one, Aegis can reach no folders: it must ask before reading
          anything, and it cannot change any file.
        </p>
      ) : (
        <ul className="flex flex-col gap-3">
          {scopes.map((scope) => (
            <li key={scope.id}>
              <ScopeCard
                scope={scope}
                busy={busy === `scope:${String(scope.id)}`}
                locked={busy !== null}
              />
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function ScopeCard({
  scope,
  busy,
  locked,
}: {
  scope: ScopeInfo;
  busy: boolean;
  locked: boolean;
}): ReactElement {
  const [name, setName] = useState(scope.name);
  const [confirming, setConfirming] = useState(false);
  const renamed = name.trim() !== '' && name.trim() !== scope.name;

  return (
    <article
      aria-label={scope.name}
      aria-busy={busy}
      className="flex flex-col gap-3 rounded-card border border-border bg-surface-1 p-4"
    >
      <form
        className="flex items-center gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (renamed) void updateScope(scope, { name: name.trim() });
        }}
      >
        <input
          type="text"
          value={name}
          maxLength={MAX_NAME}
          onChange={(event) => {
            setName(event.target.value);
          }}
          aria-label={`Name of ${scope.name}`}
          className="h-8 min-w-0 flex-1 rounded-control border border-transparent bg-transparent px-2 text-base font-medium text-text hover:border-border focus:border-border"
        />
        {renamed && (
          <Button type="submit" size="sm" disabled={locked}>
            Rename
          </Button>
        )}
      </form>

      <p className="text-sm text-text-dim">{whatThisAllows(scope)}</p>

      {scope.folders.length > 0 && (
        <ul aria-label={`Folders in ${scope.name}`} className="flex flex-col gap-1">
          {scope.folders.map((folder) => (
            <li
              key={folder}
              className="flex items-center gap-2 rounded-control bg-surface-2 px-2 py-1"
            >
              <span className="min-w-0 flex-1 truncate font-mono text-sm text-text" title={folder}>
                {folder}
              </span>
              <Button
                size="icon"
                variant="ghost"
                className="size-6"
                aria-label={`Remove ${folder} from ${scope.name}`}
                title="Remove from this scope. No files are changed."
                disabled={locked}
                onClick={() => {
                  void updateScope(scope, {
                    folders: scope.folders.filter((item) => item !== folder),
                  });
                }}
              >
                <X aria-hidden />
              </Button>
            </li>
          ))}
        </ul>
      )}

      {scope.apps.length > 0 && (
        <p className="text-sm text-text-dim">Apps: {scope.apps.join(', ')}</p>
      )}

      <div className="flex items-center gap-2">
        <Button
          size="sm"
          disabled={locked}
          onClick={() => {
            void addScopeFolder(scope.id);
          }}
        >
          <FolderPlus aria-hidden />
          {busy ? 'Working…' : 'Add folder…'}
        </Button>
        <div className="ml-auto flex items-center gap-2">
          {confirming ? (
            <>
              <span className="text-sm text-text-dim">
                Delete {scope.name}? No files are deleted.
              </span>
              <Button
                size="sm"
                variant="danger"
                disabled={locked}
                onClick={() => {
                  setConfirming(false);
                  void deleteScope(scope);
                }}
              >
                Delete scope
              </Button>
              <Button
                size="sm"
                variant="ghost"
                onClick={() => {
                  setConfirming(false);
                }}
              >
                Cancel
              </Button>
            </>
          ) : (
            <Button
              size="sm"
              variant="ghost"
              disabled={locked}
              onClick={() => {
                setConfirming(true);
              }}
            >
              <Trash2 aria-hidden />
              Delete…
            </Button>
          )}
        </div>
      </div>
    </article>
  );
}
