import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { resolveDshEntry, resolvePnpmEntry } from './dsh-runtime.js';

const cleanupPaths: string[] = [];

afterEach(async () => {
  await Promise.all(
    cleanupPaths.splice(0).map((path) => rm(path, { recursive: true, force: true })),
  );
});

async function writeRuntimePackage(
  runtimeRoot: string,
  packageName: string,
  manifest: Record<string, unknown>,
): Promise<void> {
  const packageDirectory = join(runtimeRoot, 'node_modules', packageName);
  await mkdir(packageDirectory, { recursive: true });
  await writeFile(join(packageDirectory, 'package.json'), JSON.stringify(manifest), 'utf8');
}

describe('runtime package resolution', () => {
  it('resolves the dsh executable from the packaged runtime root', async () => {
    const runtimeRoot = await mkdtemp(join(tmpdir(), 'puff-runtime-'));
    cleanupPaths.push(runtimeRoot);
    await writeRuntimePackage(runtimeRoot, '@deepseek-ai/dsh', {
      bin: { dsh: 'lib/bin.js' },
    });

    expect(resolveDshEntry({ runtimeRoot })).toBe(
      join(runtimeRoot, 'node_modules', '@deepseek-ai', 'dsh', 'lib', 'bin.js'),
    );
  });

  it('resolves the pnpm executable from the packaged runtime root', async () => {
    const runtimeRoot = await mkdtemp(join(tmpdir(), 'puff-runtime-'));
    cleanupPaths.push(runtimeRoot);
    await writeRuntimePackage(runtimeRoot, 'pnpm', {
      bin: { pnpm: 'bin/pnpm.cjs' },
    });

    expect(resolvePnpmEntry({ runtimeRoot })).toBe(
      join(runtimeRoot, 'node_modules', 'pnpm', 'bin', 'pnpm.cjs'),
    );
  });
});
