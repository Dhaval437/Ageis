// @vitest-environment node
// (jsdom rewrites `import.meta.url` to an http URL, and this test reads files.)
import { existsSync, readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { basename, dirname, join } from 'node:path';
import { fileURLToPath, URL } from 'node:url';
import { build } from 'vite';
import { describe, expect, it } from 'vitest';

/**
 * `UI.md § 2` names Inter and JetBrains Mono, and the renderer must render
 * correctly offline. These tests fail if either face stops being bundled, would
 * be fetched from anywhere but the app's own files, or is no longer the face the
 * type tokens actually ask for.
 */

const FACES = [
  { pkg: '@fontsource-variable/inter', family: 'Inter Variable', token: '--font-sans' },
  {
    pkg: '@fontsource-variable/jetbrains-mono',
    family: 'JetBrains Mono Variable',
    token: '--font-mono',
  },
] as const;

const read = (relative: string): string =>
  readFileSync(fileURLToPath(new URL(relative, import.meta.url)), 'utf8');

const css = read('../src/index.css');
const html = read('../index.html');
const require = createRequire(import.meta.url);

/** The first family in a `--font-*` declaration in `index.css`. */
function leadingFamily(token: string): string | undefined {
  const match = new RegExp(`${token}:\\s*'([^']+)'`).exec(css);
  return match?.[1];
}

describe.each(FACES)('$family', ({ pkg, family, token }) => {
  const entry = require.resolve(pkg);
  const faceCss = readFileSync(entry, 'utf8');
  const faces = faceCss.match(/@font-face\s*\{[^}]*\}/g) ?? [];

  it('is imported by index.css', () => {
    expect(css).toContain(`@import '${pkg}';`);
  });

  it('is the first family the type token asks for', () => {
    expect(leadingFamily(token)).toBe(family);
  });

  it('declares only this family, including a latin subset', () => {
    expect(faces.length).toBeGreaterThan(0);
    for (const face of faces) {
      expect(face).toContain(`font-family: '${family}';`);
    }
    expect(faceCss).toMatch(/-latin-wght-normal\.woff2/);
  });

  it('loads every face from a bundled woff2 file, never the network or a local font', () => {
    for (const face of faces) {
      const sources = [...face.matchAll(/(url|local)\(([^)]*)\)/g)];
      expect(sources.length).toBeGreaterThan(0);
      for (const [, kind, target] of sources) {
        expect(kind).toBe('url');
        expect(target).toMatch(/^\.\/files\/[\w-]+\.woff2$/);
        expect(existsSync(join(dirname(entry), target!))).toBe(true);
      }
    }
  });
});

describe('preflight defaults', () => {
  // Preflight styles `html` and `code`/`kbd`/`pre`/`samp` from these, and the
  // cleared `--font-*` namespace would otherwise leave them on Tailwind's stock stacks.
  it.each([
    ['--default-font-family', '--font-sans'],
    ['--default-mono-font-family', '--font-mono'],
  ])('points %s at %s', (name, token) => {
    expect(css).toContain(`${name}: var(${token});`);
  });
});

describe('production build', () => {
  // The source checks above cannot see what Vite does to the CSS. The first
  // hand run found one subset inlined as a `data:` URI the CSP then refused.
  it('emits every face as a woff2 file and inlines none', async () => {
    const result = await build({
      configFile: fileURLToPath(new URL('../vite.config.ts', import.meta.url)),
      logLevel: 'silent',
      build: { write: false, sourcemap: false },
    });
    const outputs = (Array.isArray(result) ? result : [result]).flatMap((r) =>
      'output' in r ? r.output : [],
    );
    const text = (name: string): string => {
      const item = outputs.find((o) => o.fileName === name);
      if (item === undefined) return '';
      return item.type === 'asset'
        ? typeof item.source === 'string'
          ? item.source
          : Buffer.from(item.source).toString('utf8')
        : item.code;
    };
    const builtCss = outputs
      .filter((o) => o.fileName.endsWith('.css'))
      .map((o) => text(o.fileName))
      .join('\n');
    const woff2 = new Set(
      outputs.filter((o) => o.fileName.endsWith('.woff2')).map((o) => basename(o.fileName)),
    );

    const sources = [...builtCss.matchAll(/@font-face\{[^}]*?src:url\(([^)]*)\)/g)].map(
      (m) => m[1]!,
    );
    const expected = FACES.flatMap(({ pkg }) =>
      [...readFileSync(require.resolve(pkg), 'utf8').matchAll(/@font-face/g)].map(() => pkg),
    );
    expect(sources).toHaveLength(expected.length);
    for (const source of sources) {
      expect(source).toMatch(/^\.\/[\w-]+\.woff2$/);
      expect(woff2.has(basename(source))).toBe(true);
    }
    expect(builtCss).not.toContain('data:font');
  }, 60_000);
});

describe('content security policy', () => {
  const policy = /http-equiv="Content-Security-Policy"\s+content="([^"]+)"/.exec(html)?.[1] ?? '';
  const directives = new Map(
    policy
      .split(';')
      .map((part) => part.trim().split(/\s+/))
      .filter((words) => words[0] !== '')
      .map(([name, ...sources]) => [name!, sources]),
  );

  it('lets fonts load only from the app itself', () => {
    expect(directives.get('default-src')).toEqual(["'self'"]);
    // Without its own directive, font-src falls back to default-src.
    expect(directives.get('font-src') ?? directives.get('default-src')).toEqual(["'self'"]);
  });
});
