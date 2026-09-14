import assert from 'node:assert/strict';
import {
  existsSync,
  mkdirSync,
  mkdtempSync,
  readFileSync,
  rmSync,
  symlinkSync,
  writeFileSync,
} from 'node:fs';
import { tmpdir } from 'node:os';
import path from 'node:path';
import { afterEach, beforeEach, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { clean, cleanTargets, WORKSPACE_DIRS } from './clean.ts';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

let root = '';

function write(relative: string, content = 'x'): string {
  const file = path.join(root, relative);
  mkdirSync(path.dirname(file), { recursive: true });
  writeFileSync(file, content);
  return file;
}

beforeEach(() => {
  root = mkdtempSync(path.join(tmpdir(), 'aegis-clean-'));
});

afterEach(() => {
  rmSync(root, { recursive: true, force: true });
});

void test('removes root and workspace node_modules, and nothing else', () => {
  write('node_modules/rimraf/package.json');
  write('apps/desktop/node_modules/.bin/tsc');
  write('packages/shared/node_modules/zod/index.js');
  const source = write('apps/desktop/src/index.ts');
  const lockfile = write('pnpm-lock.yaml');

  const removed = clean(root);

  assert.deepEqual(removed.map((target) => path.relative(root, target)).sort(), [
    path.join('apps', 'desktop', 'node_modules'),
    'node_modules',
    path.join('packages', 'shared', 'node_modules'),
  ]);
  assert.equal(existsSync(path.join(root, 'node_modules')), false);
  assert.equal(existsSync(path.join(root, 'apps', 'desktop', 'node_modules')), false);
  assert.equal(existsSync(path.join(root, 'packages', 'shared', 'node_modules')), false);
  assert.equal(existsSync(source), true);
  assert.equal(existsSync(lockfile), true);
});

void test('removes a junction into package source without following it', () => {
  const source = write('packages/shared/src/api.ts', 'export {};');
  const link = path.join(root, 'node_modules', '@aegis', 'shared');
  mkdirSync(path.dirname(link), { recursive: true });
  symlinkSync(path.join(root, 'packages', 'shared'), link, 'junction');

  clean(root);

  assert.equal(existsSync(path.join(root, 'node_modules')), false);
  assert.equal(readFileSync(source, 'utf8'), 'export {};');
});

void test('is a no-op when nothing is installed', () => {
  write('apps/desktop/package.json');
  assert.deepEqual(cleanTargets(root), []);
  assert.deepEqual(clean(root), []);
});

void test('WORKSPACE_DIRS matches pnpm-workspace.yaml', () => {
  const yaml = readFileSync(path.join(REPO, 'pnpm-workspace.yaml'), 'utf8');
  const globs = [...yaml.matchAll(/^\s*-\s*'([^']+)'\s*$/gm)].map((match) => match[1]);
  assert.deepEqual(
    globs,
    WORKSPACE_DIRS.map((dir) => `${dir}/*`),
  );
});
