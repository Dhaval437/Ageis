import { act, fireEvent, render, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BridgeResult, CoreResponse, ScopeInfo } from '@aegis/shared';
import { ScopeManager } from '@/components/ScopeManager';
import { ScopePicker } from '@/components/ScopePicker';
import { parseScope, parseScopeList, whatThisAllows } from '@/lib/scopes-api';
import {
  addScopeFolder,
  createScope,
  deleteScope,
  loadScopes,
  updateScope,
} from '@/lib/scopes-client';
import { INITIAL_SCOPES, useScopesStore } from '@/stores/scopes';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';

/** Scopes in the renderer (P3-17): only the dialog adds a folder; everything else narrows. */

const WORK: ScopeInfo = { id: 1, name: 'Work', folders: ['d:\\work', 'd:\\reports'], apps: [] };
const PHOTOS: ScopeInfo = { id: 2, name: 'Photos', folders: ['c:\\pictures'], apps: [] };

type Answer = BridgeResult<CoreResponse | null>;

function ok(status: number, body: unknown): BridgeResult<CoreResponse> {
  return { ok: true, value: { status, body } };
}

function fakeBridge(
  answers: { core?: BridgeResult<CoreResponse>; create?: Answer; addFolder?: Answer } = {},
) {
  return {
    core: {
      request: vi.fn(() => Promise.resolve(answers.core ?? ok(200, { scopes: [WORK] }))),
      subscribe: vi.fn(),
      restart: vi.fn(),
    },
    scopes: {
      create: vi.fn(() => Promise.resolve(answers.create ?? ok(200, PHOTOS))),
      addFolder: vi.fn(() =>
        Promise.resolve(
          answers.addFolder ?? ok(200, { ...WORK, folders: [...WORK.folders, 'e:\\new'] }),
        ),
      ),
    },
  };
}

beforeEach(() => {
  useScopesStore.setState({ ...INITIAL_SCOPES });
  useStreamStore.setState({ ...INITIAL_STREAM, connection: 'live' });
});

describe('parsing', () => {
  it('accepts a scope and a list of them', () => {
    expect(parseScope(WORK)).toEqual(WORK);
    expect(parseScopeList({ scopes: [WORK, PHOTOS] })).toEqual([WORK, PHOTOS]);
  });

  it.each([
    ['no id', { ...WORK, id: undefined }],
    ['id 0', { ...WORK, id: 0 }],
    ['a folder that is not text', { ...WORK, folders: [7] }],
    ['no apps', { ...WORK, apps: undefined }],
    ['not an object', 'd:\\work'],
  ])('refuses a scope with %s', (_label, value) => {
    expect(parseScope(value)).toBeNull();
    expect(parseScopeList({ scopes: [value] })).toBeNull();
  });
});

describe('whatThisAllows', () => {
  it('says what the Guardian does, for none, one and many folders', () => {
    expect(whatThisAllows({ folders: [] })).toMatch(/^Aegis can reach no folders/);
    expect(whatThisAllows({ folders: ['a'] })).toMatch(
      /in this folder\. Anywhere else it must ask before reading, and it cannot change anything/,
    );
    expect(whatThisAllows({ folders: ['a', 'b'] })).toMatch(/in these 2 folders/);
    expect(whatThisAllows({ folders: ['a'] })).not.toMatch(/\bsafe\b/i);
  });
});

describe('the client', () => {
  it('loads the list, and drops a selection that no longer exists', async () => {
    useScopesStore.setState({ selectedId: 99 });
    const bridge = fakeBridge();
    await loadScopes(bridge);
    expect(bridge.core.request).toHaveBeenCalledWith({ method: 'GET', path: '/scopes' });
    expect(useScopesStore.getState()).toMatchObject({
      status: 'ready',
      scopes: [WORK],
      selectedId: null,
    });
  });

  it('says the engine is unreachable when it is', async () => {
    await loadScopes(
      fakeBridge({ core: { ok: false, error: { code: 'unavailable', message: 'x' } } }),
    );
    expect(useScopesStore.getState()).toMatchObject({ status: 'error' });
    expect(useScopesStore.getState().loadError).toMatch(/cannot reach the engine/);
  });

  it('creates through the dialog bridge, never through core.request', async () => {
    const bridge = fakeBridge();
    await createScope('Photos', bridge);
    expect(bridge.scopes.create).toHaveBeenCalledWith('Photos');
    expect(bridge.core.request).not.toHaveBeenCalled();
    expect(useScopesStore.getState()).toMatchObject({
      scopes: [PHOTOS],
      selectedId: 2,
      busy: null,
      notice: { tone: 'info', text: 'Made the scope Photos.' },
    });
  });

  it('a cancelled dialog changes nothing and says nothing', async () => {
    await createScope('Photos', fakeBridge({ create: { ok: true, value: null } }));
    expect(useScopesStore.getState()).toMatchObject({ scopes: [], busy: null, notice: null });
  });

  it("shows the core's own sentence for a folder it refused", async () => {
    const detail = 'A whole drive cannot be a scope. Choose a folder on it.';
    await createScope('Drive', fakeBridge({ create: ok(400, { detail }) }));
    expect(useScopesStore.getState().notice).toEqual({ tone: 'error', text: detail });
  });

  it('never shows a 500 body as the reason', async () => {
    await createScope('X', fakeBridge({ create: ok(500, { detail: 'Traceback …' }) }));
    expect(useScopesStore.getState().notice?.text).toBe(
      'Aegis could not save that change. Try again.',
    );
  });

  it('adds a folder through the dialog bridge', async () => {
    useScopesStore.setState({ scopes: [WORK] });
    const bridge = fakeBridge();
    await addScopeFolder(1, bridge);
    expect(bridge.scopes.addFolder).toHaveBeenCalledWith(1);
    expect(bridge.core.request).not.toHaveBeenCalled();
    expect(useScopesStore.getState().scopes[0]?.folders).toContain('e:\\new');
  });

  it('removes a folder by sending the rest, with a PUT', async () => {
    useScopesStore.setState({ scopes: [WORK] });
    const bridge = fakeBridge({ core: ok(200, { ...WORK, folders: ['d:\\work'] }) });
    await updateScope(WORK, { folders: ['d:\\work'] }, bridge);
    expect(bridge.core.request).toHaveBeenCalledWith({
      method: 'PUT',
      path: '/scopes/1',
      body: { name: 'Work', folders: ['d:\\work'] },
    });
    expect(useScopesStore.getState().notice?.text).toBe(
      'Removed a folder from Work. No files were changed.',
    );
  });

  it('deleting the selected scope clears the selection', async () => {
    useScopesStore.setState({ scopes: [WORK, PHOTOS], selectedId: 1 });
    await deleteScope(WORK, fakeBridge({ core: ok(200, { scopes: [PHOTOS] }) }));
    expect(useScopesStore.getState()).toMatchObject({ scopes: [PHOTOS], selectedId: null });
  });
});

describe('ScopeManager', () => {
  it('has no field a folder could be typed into: only names', () => {
    useScopesStore.setState({ status: 'ready', scopes: [WORK] });
    render(<ScopeManager />);
    const labels = screen.getAllByRole('textbox').map((box) => box.getAttribute('aria-label'));
    expect(labels).toEqual(['New scope name', 'Name of Work']);
  });

  it('asks for a name before it opens the dialog', () => {
    useScopesStore.setState({ status: 'ready' });
    render(<ScopeManager />);
    const choose = screen.getByRole('button', { name: /Choose folder/ });
    expect(choose).toBeDisabled();
    fireEvent.change(screen.getByLabelText('New scope name'), { target: { value: 'Work' } });
    expect(choose).toBeEnabled();
  });

  it('removes one folder by keeping the others', async () => {
    useScopesStore.setState({ status: 'ready', scopes: [WORK] });
    const request = vi.fn(() => Promise.resolve(ok(200, { ...WORK, folders: ['d:\\work'] })));
    vi.stubGlobal('aegis', { core: { request }, scopes: fakeBridge().scopes });
    render(<ScopeManager />);
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Remove d:\\reports from Work' }));
      await Promise.resolve();
    });
    expect(request).toHaveBeenCalledWith({
      method: 'PUT',
      path: '/scopes/1',
      body: { name: 'Work', folders: ['d:\\work'] },
    });
    vi.unstubAllGlobals();
  });

  it('deletes only after a second, explicit click', () => {
    useScopesStore.setState({ status: 'ready', scopes: [WORK] });
    const request = vi.fn(() => Promise.resolve(ok(200, { scopes: [] })));
    vi.stubGlobal('aegis', { core: { request }, scopes: fakeBridge().scopes });
    render(<ScopeManager />);
    fireEvent.click(screen.getByRole('button', { name: /Delete…/ }));
    expect(request).not.toHaveBeenCalled();
    expect(screen.getByText('Delete Work? No files are deleted.')).toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Delete scope' }));
    expect(request).toHaveBeenCalledWith({ method: 'DELETE', path: '/scopes/1' });
    vi.unstubAllGlobals();
  });

  it('offers a retry when the list cannot be read', () => {
    useScopesStore.setState({ status: 'error', loadError: 'Aegis could not read your scopes.' });
    render(<ScopeManager />);
    expect(screen.getByRole('alert')).toHaveTextContent('Aegis could not read your scopes.');
    expect(screen.getByRole('button', { name: /Try again/ })).toBeEnabled();
  });
});

describe('ScopePicker', () => {
  it('shows the chosen scope, lets the person choose another, and links to Manage', () => {
    useScopesStore.setState({ status: 'ready', scopes: [PHOTOS, WORK], selectedId: 1 });
    const onManage = vi.fn();
    render(<ScopePicker onManage={onManage} />);
    fireEvent.click(screen.getByRole('button', { name: /Scope: Work/ }));
    const menu = screen.getByRole('menu');
    fireEvent.click(within(menu).getByRole('menuitemradio', { name: /Photos/ }));
    expect(useScopesStore.getState().selectedId).toBe(2);
    expect(screen.queryByRole('menu')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: /Scope: Photos/ }));
    fireEvent.click(screen.getByRole('menuitem', { name: /Manage scopes/ }));
    expect(onManage).toHaveBeenCalledOnce();
  });

  it('closes on Escape', () => {
    useScopesStore.setState({ status: 'ready', scopes: [WORK] });
    render(<ScopePicker onManage={vi.fn()} />);
    fireEvent.click(screen.getByRole('button', { name: /Scope: none/ }));
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('menu')).toBeNull();
  });

  it('cannot change the scope while a task is running, and says why', () => {
    useScopesStore.setState({ status: 'ready', scopes: [WORK], selectedId: 1 });
    useStreamStore.setState({
      events: [
        {
          seq: 1,
          ts: new Date().toISOString(),
          task_id: '7',
          type: 'task.status',
          payload: { status: 'RUNNING' },
        },
      ],
      lastSeq: 1,
    });
    render(<ScopePicker onManage={vi.fn()} />);
    const button = screen.getByRole('button', { name: /Scope: Work/ });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute('title', 'You can’t change the scope while a task is running.');
  });
});
