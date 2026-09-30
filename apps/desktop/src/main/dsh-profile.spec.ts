import { mkdir, mkdtemp, rm, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, it } from 'vitest';
import { ensureDshProfile } from './dsh-profile.js';

const cleanupPaths: string[] = [];

afterEach(async () => {
  await Promise.all(
    cleanupPaths.splice(0).map((path) => rm(path, { recursive: true, force: true })),
  );
});

describe('ensureDshProfile', () => {
  it('does nothing when the profile already exists', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-profile-'));
    cleanupPaths.push(directory);
    await mkdir(join(directory, 'profiles', 'puff'), { recursive: true });
    await writeFile(join(directory, 'profiles', 'puff', 'package.json'), '{}');

    await expect(
      ensureDshProfile({
        nodeExecutable: 'missing-node',
        dshEntry: 'missing-dsh',
        dshHome: directory,
        workspaceDir: directory,
        env: {},
      }),
    ).resolves.toBeUndefined();
  });

  it('rejects an incomplete existing profile directory', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-profile-'));
    cleanupPaths.push(directory);
    await mkdir(join(directory, 'profiles', 'puff'), { recursive: true });

    await expect(
      ensureDshProfile({
        nodeExecutable: 'missing-node',
        dshEntry: 'missing-dsh',
        dshHome: directory,
        workspaceDir: directory,
        env: {},
      }),
    ).rejects.toThrow('exists but is incomplete');
  });

  it('initializes a missing profile through the dsh CLI', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-profile-'));
    cleanupPaths.push(directory);
    const scriptPath = join(directory, 'fake-dsh.mjs');
    await writeFile(
      scriptPath,
      [
        "import { mkdir, writeFile } from 'node:fs/promises';",
        "import { join } from 'node:path';",
        "const profile = join(process.env.TEST_DSH_HOME, 'profiles', 'puff');",
        'await mkdir(profile, { recursive: true });',
        "await writeFile(join(profile, 'package.json'), '{}');",
        '',
      ].join('\n'),
      'utf8',
    );

    await ensureDshProfile({
      nodeExecutable: process.execPath,
      dshEntry: scriptPath,
      dshHome: directory,
      workspaceDir: directory,
      env: { ...process.env, TEST_DSH_HOME: directory },
    });

    await expect(
      import('node:fs/promises').then(({ readFile }) =>
        readFile(join(directory, 'profiles', 'puff', 'package.json'), 'utf8'),
      ),
    ).resolves.toBe('{}');
  });
});
