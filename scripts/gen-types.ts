/**
 * Generates `packages/shared/src/api.ts` from the Pydantic models in
 * `core/aegis_core/server/schemas.py`.
 *
 * Type-safety rule (ARCHITECTURE.md § 4): never hand-write a TS type that
 * mirrors a Python model. The rendering lives in `aegis_core.server.typegen`,
 * where it can introspect the models directly; this script only runs it,
 * formats the result with the repo's prettier config, and writes it.
 *
 *   pnpm gen:types          write the file (also run by `pnpm dev`)
 *   pnpm gen:types:check    exit 1 if the committed file is stale (run by `pnpm test`)
 *
 * Runs on Node's built-in type stripping, so it uses erasable syntax only.
 */

import { spawnSync } from 'node:child_process';
import { existsSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import prettier from 'prettier';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const CORE = path.join(ROOT, 'core');
const PYTHON = path.join(CORE, '.venv', 'Scripts', 'python.exe');
const TARGET = path.join(ROOT, 'packages', 'shared', 'src', 'api.ts');
const TIMEOUT_MS = 60_000;

function fail(message: string): never {
  console.error(`gen-types: ${message}`);
  process.exit(1);
}

function renderFromPython(): string {
  if (!existsSync(PYTHON)) {
    fail(`no core interpreter at ${PYTHON}. Create the venv first (see core/README.md).`);
  }
  const result = spawnSync(PYTHON, ['-m', 'aegis_core.server.typegen'], {
    cwd: CORE,
    encoding: 'utf8',
    timeout: TIMEOUT_MS,
    windowsHide: true,
  });
  if (result.error) {
    fail(`could not run the generator: ${result.error.message}`);
  }
  if (result.status !== 0) {
    fail(`the generator exited with ${String(result.status)}:\n${result.stderr}`);
  }
  return result.stdout;
}

async function main(): Promise<void> {
  const check = process.argv.includes('--check');
  const options = (await prettier.resolveConfig(TARGET)) ?? {};
  const generated = await prettier.format(renderFromPython(), { ...options, filepath: TARGET });
  const current = existsSync(TARGET) ? readFileSync(TARGET, 'utf8') : null;

  if (current === generated) {
    return;
  }
  if (check) {
    fail(`${path.relative(ROOT, TARGET)} is stale. Run \`pnpm gen:types\` and commit the result.`);
  }
  writeFileSync(TARGET, generated, 'utf8');
  console.warn(`gen-types: wrote ${path.relative(ROOT, TARGET)}`);
}

await main();
