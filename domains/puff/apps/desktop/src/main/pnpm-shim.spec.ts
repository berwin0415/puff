import { mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { ensurePnpmShim } from './pnpm-shim.js';

const cleanupPaths: string[] = [];

afterEach(async () => {
  await Promise.all(
    cleanupPaths.splice(0).map((path) => rm(path, { recursive: true, force: true })),
  );
});

describe('ensurePnpmShim', () => {
  it('writes a Windows command shim that runs pnpm through Electron Node mode', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-pnpm-shim-'));
    cleanupPaths.push(directory);

    const shimDirectory = await ensurePnpmShim({
      userDataDir: directory,
      nodeExecutable: 'C:\\Program Files\\puff\\puff.exe',
      pnpmEntry: 'C:\\Program Files\\puff\\pnpm.cjs',
      platform: 'win32',
    });
    const contents = await readFile(join(shimDirectory, 'pnpm.cmd'), 'utf8');

    expect(shimDirectory).toBe(join(directory, 'bin'));
    expect(contents).toContain('ELECTRON_RUN_AS_NODE=1');
    expect(contents).toContain('"%PUFF_PNPM_NODE%" "%PUFF_PNPM_ENTRY%" %*');
  });

  it('writes an executable POSIX shim', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-pnpm-shim-'));
    cleanupPaths.push(directory);

    const shimDirectory = await ensurePnpmShim({
      userDataDir: directory,
      nodeExecutable: '/opt/puff/puff',
      pnpmEntry: '/opt/puff/pnpm.cjs',
      platform: 'linux',
    });
    const shimPath = join(shimDirectory, 'pnpm');

    expect(await readFile(shimPath, 'utf8')).toContain(
      'ELECTRON_RUN_AS_NODE=1 exec "$PUFF_PNPM_NODE" "$PUFF_PNPM_ENTRY" "$@"',
    );
    expect((await stat(shimPath)).isFile()).toBe(true);
  });
});
