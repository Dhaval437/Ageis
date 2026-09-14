/**
 * Removes every installed `node_modules` in the repo: the root one and each
 * workspace package's. Run by `pnpm clean` after `turbo run clean`.
 *
 * This cannot be `rimraf node_modules`: rimraf is installed *in* that folder,
 * so it deleted itself partway through and always exited 1 half-done. This
 * script uses Node built-ins only, so nothing it needs lives in what it deletes.
 *
 * pnpm links workspace packages into `node_modules` with junctions.
 * `fs.rmSync` removes a link without following it, so the package source it
 * points at is never touched; `scripts/clean.test.ts` holds it to that.
 *
 * Runs on Node's built-in type stripping, so it uses erasable syntax only.
 */

import { existsSync, readdirSync, rmSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

/** Folders holding workspace packages. Must match `pnpm-workspace.yaml`, which a test checks. */
export const WORKSPACE_DIRS = ['apps', 'packages'] as const;

/** Every `node_modules` folder that `clean` removes under `root`, root first. */
export function cleanTargets(root: string): string[] {
  const targets = [path.join(root, 'node_modules')];
  for (const dir of WORKSPACE_DIRS) {
    const parent = path.join(root, dir);
    if (!existsSync(parent)) {
      continue;
    }
    for (const entry of readdirSync(parent, { withFileTypes: true })) {
      if (entry.isDirectory()) {
        targets.push(path.join(parent, entry.name, 'node_modules'));
      }
    }
  }
  return targets.filter((target) => existsSync(target));
}

/** Removes the targets and returns what was removed. Throws on the first failure. */
export function clean(root: string): string[] {
  const targets = cleanTargets(root);
  for (const target of targets) {
    // Retries cover Windows' transient EBUSY/EPERM from AV scanners and indexers.
    rmSync(target, { recursive: true, force: true, maxRetries: 5, retryDelay: 200 });
  }
  return targets;
}

function main(): void {
  const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
  try {
    for (const target of clean(root)) {
      console.warn(`clean: removed ${path.relative(root, target)}`);
    }
  } catch (error) {
    console.error(`clean: ${error instanceof Error ? error.message : String(error)}`);
    process.exit(1);
  }
}

if (
  process.argv[1] !== undefined &&
  path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)
) {
  main();
}
