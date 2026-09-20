import { describe, expect, it } from 'vitest';
import { CATALOG, CONFIGURED, SPEND } from '../.storybook/models-fixtures';
import {
  coreDetail,
  parseCatalog,
  parseSettings,
  parseSpend,
  parseSpendTotals,
  parseValidate,
} from '@/lib/models-api';

/**
 * The bridge hands the renderer an `unknown` body, so nothing reaches a store
 * without passing through here. A shape that does not match is `null` — the
 * screen shows its error state rather than rendering half a catalogue.
 */

/** Round-tripping the fixtures proves the parsers accept what the core sends. */
function json(value: unknown): unknown {
  return JSON.parse(JSON.stringify(value)) as unknown;
}

describe('parseCatalog', () => {
  it('accepts what the core sends', () => {
    expect(parseCatalog(json(CATALOG))).toEqual(CATALOG);
  });

  it('rejects anything that is not a catalogue', () => {
    for (const value of [null, 7, 'x', [], {}, { providers: {} }]) {
      expect(parseCatalog(value)).toBeNull();
    }
  });

  it('rejects a provider with an id this build does not know', () => {
    const catalog = json(CATALOG) as { providers: { provider_id: string }[] };
    const first = catalog.providers[0];
    if (first === undefined) throw new TypeError('the fixture has no providers');
    first.provider_id = 'some-new-provider';
    expect(parseCatalog(catalog)).toBeNull();
  });

  it('rejects the whole catalogue if one model is malformed', () => {
    const catalog = json(CATALOG) as { providers: { models: unknown[] }[] };
    const first = catalog.providers[0];
    if (first === undefined) throw new TypeError('the fixture has no providers');
    first.models = [{ id: 'gpt-4o' }];
    expect(parseCatalog(catalog)).toBeNull();
  });

  it('keeps an unknown price as null rather than turning it into a number', () => {
    const catalog = json(CATALOG) as {
      providers: { models: { cost_per_mtok_input: number | null }[] }[];
    };
    const model = catalog.providers[0]?.models[0];
    if (model === undefined) throw new TypeError('the fixture has no models');
    model.cost_per_mtok_input = null;
    expect(parseCatalog(catalog)?.providers[0]?.models[0]?.cost_per_mtok_input).toBeNull();
  });
});

describe('parseSettings', () => {
  it('accepts a fully mapped configuration', () => {
    expect(parseSettings(json({ models: CONFIGURED }))).toEqual(CONFIGURED);
  });

  it('accepts an unmapped role as null, which is what first run looks like', () => {
    const settings = parseSettings(json({ models: { ...CONFIGURED, planner: null } }));
    expect(settings?.planner).toBeNull();
  });

  it('rejects a role that is not a route', () => {
    expect(parseSettings(json({ models: { ...CONFIGURED, planner: 'gpt-4o' } }))).toBeNull();
  });

  it('rejects a body with no models section', () => {
    expect(parseSettings(json(CONFIGURED))).toBeNull();
  });

  it('keeps a cleared ceiling as null, never as zero', () => {
    const body = json({
      models: { ...CONFIGURED, limits: { ...CONFIGURED.limits, day_cents: null } },
    });
    expect(parseSettings(body)?.limits.day_cents).toBeNull();
  });
});

describe('parseSpend', () => {
  it('accepts what the core sends', () => {
    expect(parseSpend(json(SPEND))).toEqual(SPEND);
  });

  it('rejects totals that are not numbers', () => {
    expect(parseSpendTotals({ cents: '1', tokens: 0, unpriced_calls: 0 })).toBeNull();
    expect(parseSpendTotals({ cents: Number.NaN, tokens: 0, unpriced_calls: 0 })).toBeNull();
  });
});

describe('parseValidate', () => {
  it('accepts a result', () => {
    const result = {
      provider_id: 'openai',
      valid: true,
      detail: 'That key works.',
      latency_ms: 12,
    };
    expect(parseValidate(result)).toEqual(result);
  });

  it('rejects a result missing its detail', () => {
    expect(parseValidate({ provider_id: 'openai', valid: true, latency_ms: 12 })).toBeNull();
  });
});

describe('coreDetail', () => {
  it('passes a 4xx sentence through, because the core wrote it for the user', () => {
    expect(coreDetail(400, { detail: 'Use https:// for an address off this machine.' })).toBe(
      'Use https:// for an address off this machine.',
    );
  });

  it('never shows a 5xx body, which is written for a developer', () => {
    expect(coreDetail(500, { detail: 'Traceback (most recent call last)' })).toBeNull();
  });

  it('ignores an empty or missing detail', () => {
    expect(coreDetail(400, { detail: '   ' })).toBeNull();
    expect(coreDetail(404, {})).toBeNull();
    expect(coreDetail(400, 'nope')).toBeNull();
  });
});
