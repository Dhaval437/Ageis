// @vitest-environment node
// (jsdom rewrites `import.meta.url` to an http URL, and this test reads a file.)
import { readFileSync } from 'node:fs';
import { fileURLToPath, URL } from 'node:url';
import { describe, expect, it } from 'vitest';

/**
 * `UI.md § 2` is binding. These tests fail if a token is dropped, renamed, or
 * defined for only one theme — the specific mistake the doc calls out ("both
 * themes defined, never define a colour only inside a media query").
 */

const TOKENS = [
  '--bg',
  '--surface-1',
  '--surface-2',
  '--border',
  '--text',
  '--text-dim',
  '--accent',
  '--safe',
  '--caution',
  '--danger',
  '--forbidden',
] as const;

const css = readFileSync(fileURLToPath(new URL('../src/index.css', import.meta.url)), 'utf8');

/** The declarations of one `{ … }` block, keyed by custom-property name. */
function block(selector: string): Map<string, string> {
  const start = css.indexOf(selector);
  expect(start, `selector not found: ${selector}`).toBeGreaterThan(-1);
  const open = css.indexOf('{', start);
  const close = css.indexOf('}', open);
  const declarations = new Map<string, string>();
  for (const line of css.slice(open + 1, close).split(';')) {
    const [name, value] = line.split(':', 2);
    if (name !== undefined && value !== undefined && name.trim().startsWith('--')) {
      declarations.set(name.trim(), value.trim());
    }
  }
  return declarations;
}

describe('design tokens', () => {
  const dark = block(":root,\n:root[data-theme='dark']");
  const light = block(":root[data-theme='light']");
  const systemLight = block(":root:not([data-theme='dark'])");

  it.each(TOKENS)('defines %s in the dark theme', (token) => {
    expect(dark.get(token)).toMatch(/^#[0-9a-f]{6}$/);
  });

  it.each(TOKENS)('defines %s in the light theme', (token) => {
    expect(light.get(token)).toMatch(/^#[0-9a-f]{6}$/);
  });

  it('keeps the OS-preference light palette identical to the explicit one', () => {
    expect(Object.fromEntries(systemLight)).toEqual(Object.fromEntries(light));
  });

  it('matches the dark palette in UI.md § 2 exactly', () => {
    expect(Object.fromEntries(dark)).toEqual({
      '--bg': '#0b0d10',
      '--surface-1': '#12151a',
      '--surface-2': '#191d24',
      '--border': '#252b34',
      '--text': '#e6eaf0',
      '--text-dim': '#8a94a6',
      '--accent': '#4c8dff',
      '--safe': '#3dd68c',
      '--caution': '#f5b547',
      '--danger': '#ff5c5c',
      '--forbidden': '#b14cff',
    });
  });

  it('exposes every token to Tailwind as a colour utility', () => {
    for (const token of TOKENS) {
      expect(css).toContain(`--color-${token.slice(2)}: var(${token});`);
    }
  });
});
