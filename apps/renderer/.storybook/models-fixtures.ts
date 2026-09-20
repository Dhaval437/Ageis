import type {
  ModelCatalog,
  ModelInfo,
  ModelSettings,
  ProviderCatalog,
  ProviderId,
  RoleRouteSpec,
  SpendResponse,
} from '@aegis/shared';

/**
 * A catalogue, a configuration and a spend, shaped exactly as the core answers
 * (`P1-10`).
 *
 * Shared by the Models stories and the Models tests so both show the same thing,
 * and so a change to the wire shape breaks one place rather than eight. Nothing
 * here is a key: `masked_key` is the only form of a saved key that exists in the
 * renderer at all.
 */

function model(id: string, overrides: Partial<ModelInfo> = {}): ModelInfo {
  return {
    id,
    vision: true,
    tool_calling: true,
    json_mode: true,
    ctx_window: 128_000,
    cost_per_mtok_input: 2.5,
    cost_per_mtok_output: 10,
    ...overrides,
  };
}

function provider(
  provider_id: ProviderId,
  label: string,
  overrides: Partial<ProviderCatalog> = {},
): ProviderCatalog {
  return {
    provider_id,
    label,
    local: false,
    requires_key: true,
    has_key: false,
    masked_key: null,
    base_url: null,
    editable_base_url: false,
    models: [],
    free_text_model: false,
    detail: null,
    ...overrides,
  };
}

export const CATALOG: ModelCatalog = {
  providers: [
    provider('openai', 'OpenAI', {
      has_key: true,
      masked_key: 'sk-…mnop',
      models: [model('gpt-4o'), model('gpt-4o-mini', { cost_per_mtok_input: 0.15 })],
    }),
    provider('anthropic', 'Anthropic', {
      free_text_model: true,
      models: [model('claude-sonnet-4-5')],
    }),
    provider('openrouter', 'OpenRouter', {
      free_text_model: true,
      detail:
        "Aegis can't list this provider's models. Type the model id exactly as the provider spells it.",
    }),
    provider('ollama', 'Local (Ollama)', {
      local: true,
      requires_key: false,
      editable_base_url: true,
      base_url: 'http://127.0.0.1:11434',
      models: [
        model('llama3.1:8b', {
          vision: false,
          ctx_window: 4096,
          cost_per_mtok_input: 0,
          cost_per_mtok_output: 0,
        }),
      ],
    }),
    provider('custom', 'Custom gateway', {
      editable_base_url: true,
      free_text_model: true,
      detail: 'Add the address of your gateway, then Test it.',
    }),
  ],
};

export function route(providerId: ProviderId, model: string): RoleRouteSpec {
  return {
    primary: { provider_id: providerId, model, temperature: null, max_output_tokens: null },
    fallbacks: [],
  };
}

export const LIMITS: ModelSettings['limits'] = {
  task_cents: 100,
  day_cents: 1000,
  task_tokens: 10_000_000,
  day_tokens: 100_000_000,
};

/** First run: nothing mapped, no keys, the default ceilings. */
export const UNCONFIGURED: ModelSettings = {
  planner: null,
  grounder: null,
  utility: null,
  limits: LIMITS,
  custom_base_url: null,
  ollama_base_url: null,
};

/** Every role mapped, which is the state a task can actually run in. */
export const CONFIGURED: ModelSettings = {
  ...UNCONFIGURED,
  planner: route('openai', 'gpt-4o'),
  grounder: route('openai', 'gpt-4o-mini'),
  utility: route('ollama', 'llama3.1:8b'),
};

export const SPEND: SpendResponse = {
  day: { cents: 250, tokens: 128_400, unpriced_calls: 0 },
  limits: LIMITS,
};
