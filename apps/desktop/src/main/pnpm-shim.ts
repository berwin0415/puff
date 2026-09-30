import { chmod, mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

export interface PnpmShimOptions {
  userDataDir: string;
  nodeExecutable: string;
  pnpmEntry: string;
  platform?: NodeJS.Platform;
}

export function pnpmShimDir(userDataDir: string): string {
  return join(userDataDir, 'bin');
}

export async function ensurePnpmShim(options: PnpmShimOptions): Promise<string> {
  const platform = options.platform ?? process.platform;
  const directory = pnpmShimDir(options.userDataDir);
  await mkdir(directory, { recursive: true });

  if (platform === 'win32') {
    const commandPath = join(directory, 'pnpm.cmd');
    await writeFile(
      commandPath,
      [
        '@echo off',
        'setlocal',
        'set "ELECTRON_RUN_AS_NODE=1"',
        '"%PUFF_PNPM_NODE%" "%PUFF_PNPM_ENTRY%" %*',
        'exit /b %ERRORLEVEL%',
        '',
      ].join('\r\n'),
      'utf8',
    );
    return directory;
  }

  const commandPath = join(directory, 'pnpm');
  await writeFile(
    commandPath,
    ['#!/bin/sh', 'ELECTRON_RUN_AS_NODE=1 exec "$PUFF_PNPM_NODE" "$PUFF_PNPM_ENTRY" "$@"', ''].join(
      '\n',
    ),
    'utf8',
  );
  await chmod(commandPath, 0o755);
  return directory;
}
