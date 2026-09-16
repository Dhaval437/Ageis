/**
 * Drift tests for `.github/workflows/ci.yml` (PROGRESS.md P0-18).
 *
 * CI is the one check that cannot fail on the machine that changes it, so the
 * things it silently gets wrong — a renamed script, a moved venv, a Node or
 * Python version the workspace no longer supports — are asserted here instead.
 *
 * Deliberately a text scan, not a YAML parse: the root has no YAML parser and
 * adding one for four assertions is a dependency with one caller.
 *
 * Runs on Node's built-in type stripping (`pnpm scripts:test`).
 */

import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');

const WORKFLOW = readFileSync(path.join(REPO, '.github', 'workflows', 'ci.yml'), 'utf8');
const PACKAGE_JSON = JSON.parse(
  readFileSync(path.join(REPO, 'package.json'), 'utf8'),
) as PackageJson;
const PYPROJECT = readFileSync(path.join(REPO, 'core', 'pyproject.toml'), 'utf8');

/** How `py:lint` and friends spell the interpreter, relative to `core/`. */
const VENV_PYTHON = '.venv\\Scripts\\python.exe';

interface PackageJson {
  engines: { node: string };
  scripts: Record<string, string>;
}

/** The commands the workflow's `run:` steps execute, one per line. */
function runCommands(source: string): string[] {
  const lines = source.split('\n');
  const commands: string[] = [];

  // Set while walking the lines of a `run: |` block, to the indentation of the
  // `run:` key itself; the block ends at the first line indented no further.
  let blockIndent: number | null = null;

  for (const line of lines) {
    if (blockIndent !== null) {
      if (line.trim() === '') {
        continue;
      }
      if (line.search(/\S/) > blockIndent) {
        commands.push(line.trim());
        continue;
      }
      blockIndent = null;
    }

    const block = /^(\s*)(?:- )?run: \|\s*$/.exec(line);
    if (block?.[1] !== undefined) {
      blockIndent = block[1].length;
      continue;
    }
    const inline = /^\s*(?:- )?run: (?!\|)(.+)$/.exec(line);
    if (inline?.[1] !== undefined) {
      commands.push(inline[1].trim());
    }
  }
  return commands;
}

/** The root `pnpm <script>` names the workflow runs. */
function pnpmScripts(source: string): string[] {
  return runCommands(source)
    .map((command) => /^pnpm (?:run )?([\w:]+)/.exec(command)?.[1])
    .filter((name): name is string => name !== undefined && name !== 'install');
}

void test('runs on Windows — the only platform AEGIS supports', () => {
  assert.match(WORKFLOW, /^ {4}runs-on: windows-latest$/m);
});

void test('installs from the lockfile, so CI cannot resolve a version the repo did not', () => {
  assert.ok(
    runCommands(WORKFLOW).includes('pnpm install --frozen-lockfile'),
    'expected a `pnpm install --frozen-lockfile` step',
  );
});

void test('every pnpm script it runs exists in the root package.json', () => {
  const missing = pnpmScripts(WORKFLOW).filter((name) => !(name in PACKAGE_JSON.scripts));
  assert.deepEqual(missing, [], `workflow runs scripts the root package.json does not define`);
});

void test('runs the whole gate: build, both lints, both typechecks, and the suite', () => {
  const run = new Set(pnpmScripts(WORKFLOW));
  for (const name of ['build', 'lint', 'typecheck', 'py:lint', 'py:typecheck', 'test']) {
    assert.ok(run.has(name), `the workflow never runs \`pnpm ${name}\``);
  }
});

void test('creates the venv where the py: scripts and the type generator look for it', () => {
  // `py:lint` and friends spell `.venv\Scripts\python.exe` relative to `core/`,
  // and `scripts/gen-types.ts` resolves `core/.venv/Scripts/python.exe`.
  assert.match(WORKFLOW, /^ {8}working-directory: core$/m);
  assert.ok(
    runCommands(WORKFLOW).includes('python -m venv .venv'),
    'expected the venv to be created at core/.venv',
  );
  assert.ok(
    runCommands(WORKFLOW).includes(`${VENV_PYTHON} -m pip install -e '.[dev]'`),
    'expected the core to be installed into core/.venv with its dev extra',
  );
  for (const script of ['py:lint', 'py:typecheck', 'py:test']) {
    assert.ok(
      PACKAGE_JSON.scripts[script]?.includes(VENV_PYTHON),
      `\`${script}\` no longer runs ${VENV_PYTHON}`,
    );
  }
});

void test('pins a Node major the workspace still declares support for', () => {
  const workflow = /^ {10}node-version: '?(\d+)\.x'?$/m.exec(WORKFLOW)?.[1];
  const engines = /^>=(\d+)\./.exec(PACKAGE_JSON.engines.node)?.[1];
  assert.ok(workflow !== undefined, 'the workflow does not pin a Node major');
  assert.equal(workflow, engines, 'CI Node major and package.json engines.node disagree');
});

void test('pins the Python version the core targets', () => {
  const workflow = /^ {10}python-version: '(\d+\.\d+)'$/m.exec(WORKFLOW)?.[1];
  const lowerBound = /^requires-python = ">=(\d+\.\d+),/m.exec(PYPROJECT)?.[1];
  assert.ok(workflow !== undefined, 'the workflow does not pin a Python version');
  assert.equal(workflow, lowerBound, 'CI Python and core/pyproject.toml requires-python disagree');
});
