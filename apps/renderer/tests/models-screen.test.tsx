import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import type { BridgeResult, CoreRequest, CoreResponse, StreamEvent } from '@aegis/shared';
import { CATALOG, CONFIGURED, LIMITS, SPEND, UNCONFIGURED } from '../.storybook/models-fixtures';
import { AppShell } from '@/components/AppShell';
import { ModelsScreen } from '@/components/models/ModelsScreen';
import { INITIAL_MODELS, useModelsStore, type ModelsSnapshot } from '@/stores/models';
import { INITIAL_STREAM, useStreamStore } from '@/stores/stream';
import { INITIAL_WINDOW, useWindowStore } from '@/stores/window';

/**
 * The Models screen as a user meets it (`UI.md § 8.4`).
 *
 * The rules being checked are the screen's promises rather than its markup: a
 * saved key is only ever shown masked, the local provider carries the badge that
 * is the point of it, a role with no model says so instead of failing later, and
 * the spend meter follows the stream rather than a poll.
 */

const FAKE_KEY = 'sk-proj-0123456789abcdefghijklmnop';

const unavailable: BridgeResult<CoreResponse> = {
  ok: false,
  error: { code: 'unavailable', message: 'no core' },
};

const core = {
  request: vi.fn((_request: CoreRequest): Promise<BridgeResult<CoreResponse>> =>
    Promise.resolve(unavailable),
  ),
  subscribe: vi.fn(() => () => undefined),
  restart: vi.fn(),
};

function ready(state: Partial<ModelsSnapshot> = {}): void {
  useModelsStore.setState({
    ...INITIAL_MODELS,
    status: 'ready',
    catalog: CATALOG,
    saved: CONFIGURED,
    draft: CONFIGURED,
    spend: SPEND,
    ...state,
  });
}

function costEvent(cents: number): StreamEvent {
  return {
    seq: 1,
    ts: '2026-09-20T12:00:00.000Z',
    task_id: null,
    type: 'cost.updated',
    payload: {
      day: { cents, tokens: 900, unpriced_calls: 0 },
      task: { cents: 0, tokens: 0, unpriced_calls: 0 },
      limits: { task_cents: 100, day_cents: 1000, task_tokens: null, day_tokens: null },
      breach: null,
    },
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  core.request.mockResolvedValue(unavailable);
  useModelsStore.setState(INITIAL_MODELS);
  useStreamStore.setState(INITIAL_STREAM);
  useWindowStore.setState(INITIAL_WINDOW);
  vi.stubGlobal('aegis', {
    core,
    app: { logsPath: vi.fn(), copyDiagnosticReport: vi.fn() },
    system: { openPath: vi.fn() },
    window: { minimize: vi.fn(), maximize: vi.fn(), close: vi.fn() },
  });
});

describe('ModelsScreen', () => {
  it('shows the three roles from ARCHITECTURE.md § 5.2', () => {
    ready();
    render(<ModelsScreen />);
    for (const label of ['Planner', 'Grounder', 'Utility']) {
      expect(screen.getByRole('heading', { name: label })).toBeVisible();
    }
  });

  it('names the roles that have no model, rather than failing at the first task', () => {
    ready({ saved: UNCONFIGURED, draft: UNCONFIGURED });
    render(<ModelsScreen />);
    expect(screen.getByText(/Planner and Grounder and Utility have no model yet/)).toBeVisible();
  });

  it('says nothing about missing roles once all three are mapped', () => {
    ready();
    render(<ModelsScreen />);
    expect(screen.queryByText(/no model yet/)).not.toBeInTheDocument();
  });

  it('shows a saved key only as its mask, never as itself', () => {
    ready();
    const { container } = render(<ModelsScreen />);
    expect(screen.getByText('sk-…mnop')).toBeVisible();
    expect(container.textContent).not.toContain(FAKE_KEY);
  });

  it('gives the key field a password input, so nothing keeps a visible copy', () => {
    ready();
    const { container } = render(<ModelsScreen />);
    const field = container.querySelector('#openai-key');
    expect(field).toHaveAttribute('type', 'password');
    expect(field).toHaveAttribute('autocomplete', 'off');
  });

  it('carries the UI.md § 8.4 badge on the local provider and on no other', () => {
    ready();
    render(<ModelsScreen />);
    const badges = screen.getAllByText('Nothing leaves your PC');
    expect(badges).toHaveLength(1);
    const local = screen.getByRole('region', { name: 'Local (Ollama)' });
    expect(within(local).getByText('Nothing leaves your PC')).toBeVisible();
    expect(within(local).getByText(/needs no key/)).toBeVisible();
  });

  it('offers a model dropdown where the catalogue knows the models', () => {
    ready();
    const { container } = render(<ModelsScreen />);
    expect(container.querySelector('select#planner-0-model')).toBeInTheDocument();
  });

  it('offers a text field where the provider cannot be enumerated', () => {
    ready({
      draft: {
        ...CONFIGURED,
        planner: {
          primary: {
            provider_id: 'openrouter',
            model: 'meta-llama/llama-3.1-70b',
            temperature: null,
            max_output_tokens: null,
          },
          fallbacks: [],
        },
      },
    });
    const { container } = render(<ModelsScreen />);
    const field = container.querySelector('input#planner-0-model');
    expect(field).toBeInTheDocument();
    expect(field).toHaveValue('meta-llama/llama-3.1-70b');
  });

  it('keeps a model the catalogue no longer lists, and says what is wrong', () => {
    ready({
      catalog: {
        providers: CATALOG.providers.map((provider) =>
          provider.provider_id === 'openai' ? { ...provider, models: [] } : provider,
        ),
      },
    });
    const { container } = render(<ModelsScreen />);
    expect(container.querySelector('input#planner-0-model')).toHaveValue('gpt-4o');
  });
});

describe('the Save button', () => {
  it('is dead until something changes, so it never claims there is work to do', () => {
    ready();
    render(<ModelsScreen />);
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeDisabled();
  });

  it('wakes up on an edit and says there are unsaved changes', () => {
    ready();
    render(<ModelsScreen />);
    fireEvent.change(screen.getByLabelText(/Per day/), { target: { value: '2.5' } });
    expect(screen.getByRole('button', { name: 'Save changes' })).toBeEnabled();
    expect(screen.getByText('Unsaved changes.')).toBeVisible();
  });

  it('stores money in cents, because that is what the ledger counts', () => {
    ready();
    render(<ModelsScreen />);
    fireEvent.change(screen.getByLabelText(/Per day/), { target: { value: '2.5' } });
    expect(useModelsStore.getState().draft?.limits.day_cents).toBe(250);
  });

  it('reads a cleared ceiling as no limit, never as zero', () => {
    ready();
    render(<ModelsScreen />);
    fireEvent.change(screen.getByLabelText(/Per day/), { target: { value: '' } });
    expect(useModelsStore.getState().draft?.limits.day_cents).toBeNull();
  });

  it('keeps both changes when two land in one React batch', () => {
    // Found by driving the real app: three role cards were each given a model in
    // one tick, and only the last survived, because every handler closed over
    // the draft it had been rendered with.
    ready({ saved: UNCONFIGURED, draft: UNCONFIGURED });
    render(<ModelsScreen />);
    act(() => {
      for (const role of ['Planner', 'Grounder', 'Utility']) {
        fireEvent.click(screen.getByRole('button', { name: `Choose a model for ${role}` }));
      }
    });
    const draft = useModelsStore.getState().draft;
    expect(draft?.planner).not.toBeNull();
    expect(draft?.grounder).not.toBeNull();
    expect(draft?.utility).not.toBeNull();
  });

  it('throws unsaved changes away on Discard', () => {
    ready();
    render(<ModelsScreen />);
    fireEvent.change(screen.getByLabelText(/Per day/), { target: { value: '2.5' } });
    fireEvent.click(screen.getByRole('button', { name: 'Discard' }));
    expect(useModelsStore.getState().draft).toEqual(CONFIGURED);
  });
});

describe('the spend meter', () => {
  it('starts from what the screen loaded with', () => {
    ready();
    render(<ModelsScreen />);
    const meter = screen.getByRole('region', { name: 'Spend today' });
    expect(within(meter).getByText('$2.50')).toBeVisible();
  });

  it('follows the stream, without this screen asking again', () => {
    ready();
    render(<ModelsScreen />);
    act(() => {
      useStreamStore.getState().apply({ kind: 'event', event: costEvent(900) });
    });
    const meter = screen.getByRole('region', { name: 'Spend today' });
    expect(within(meter).getByText('$9.00')).toBeVisible();
    expect(core.request).not.toHaveBeenCalled();
  });

  it('says a task will pause once the ceiling is reached', () => {
    ready({ spend: { day: { cents: 1000, tokens: 10, unpriced_calls: 0 }, limits: LIMITS } });
    render(<ModelsScreen />);
    expect(screen.getByText(/Daily limit reached/)).toBeVisible();
  });

  it('says *at least* when a price is missing', () => {
    ready({ spend: { day: { cents: 12, tokens: 10, unpriced_calls: 3 }, limits: LIMITS } });
    render(<ModelsScreen />);
    expect(screen.getByText('at least $0.12')).toBeVisible();
  });
});

describe('loading and failing', () => {
  it('loads once when it is first shown', async () => {
    render(<ModelsScreen />);
    await waitFor(() => {
      expect(useModelsStore.getState().status).toBe('error');
    });
    const paths = core.request.mock.calls.map(([request]) => request.path);
    expect(new Set(paths)).toEqual(new Set(['/models/catalog', '/settings', '/models/spend']));
  });

  it('shows why it failed, and how to try again', async () => {
    render(<ModelsScreen />);
    await screen.findByRole('heading', { name: 'Aegis could not load your model settings' });
    expect(screen.getByText(/cannot reach the engine/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Try again' })).toBeEnabled();
  });

  it('shows skeletons rather than a spinner while it loads', () => {
    useModelsStore.setState({ ...INITIAL_MODELS, status: 'loading' });
    render(<ModelsScreen />);
    expect(screen.getByLabelText('Loading your model settings')).toBeVisible();
  });
});

describe('the rail', () => {
  it('opens the Models screen from the Models destination', () => {
    ready();
    render(<AppShell />);
    expect(screen.queryByRole('heading', { name: 'Models' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Models' }));
    expect(screen.getByRole('heading', { name: 'Models' })).toBeVisible();
  });

  it('gives way to the Engine-unavailable screen when the engine is gone', () => {
    ready();
    render(<AppShell />);
    fireEvent.click(screen.getByRole('button', { name: 'Models' }));
    act(() => {
      useStreamStore.getState().apply({ kind: 'connection', state: 'unavailable' });
    });
    expect(screen.queryByRole('heading', { name: 'Models' })).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'The Aegis engine isn’t running' })).toBeVisible();
  });
});
