import { spawn } from 'node:child_process';
import { readFileSync } from 'node:fs';
import { chmod, cp, mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises';
import { createRequire } from 'node:module';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const appDirectory = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const outputDirectory = join(appDirectory, 'release', 'desktop');
const target = process.argv[2] === 'dir' ? 'dir' : 'nsis';
const require = createRequire(import.meta.url);
const pnpmManifestPath = require.resolve('pnpm');
const pnpmManifest = JSON.parse(readFileSync(pnpmManifestPath, 'utf8'));
const pnpmCli = resolve(dirname(pnpmManifestPath), pnpmManifest.bin.pnpm);

function run(command, args, options = {}) {
  return new Promise((resolvePromise, reject) => {
    const child = spawn(command, args, {
      cwd: options.cwd ?? appDirectory,
      env: options.env ?? process.env,
      stdio: 'inherit',
    });
    child.once('error', reject);
    child.once('exit', (code) => {
      if (code === 0) {
        resolvePromise();
      } else {
        reject(new Error(`${command} exited with code ${String(code)}`));
      }
    });
  });
}

async function prepareStagingDirectory(stagingDirectory, runtimeDirectory) {
  await mkdir(stagingDirectory, { recursive: true });
  await writeFile(
    join(runtimeDirectory, 'package.json'),
    `${JSON.stringify(
      {
        name: 'puff-dsh-runtime-build',
        private: true,
        dependencies: {
          '@deepseek-ai/dsh': '0.1.7-rc.2',
          pnpm: '11.7.0',
        },
      },
      null,
      2,
    )}\n`,
    'utf8',
  );
  await writeFile(
    join(runtimeDirectory, 'pnpm-workspace.yaml'),
    [
      'packages: []',
      '',
      'allowBuilds:',
      "  '@deepseek-ai/dsh-subprocess-local': true",
      "  '@google/genai': false",
      '  electron-winstaller: false',
      '  esbuild: true',
      '  koffi: true',
      '  node-pty: true',
      '  protobufjs: true',
      '',
    ].join('\n'),
    'utf8',
  );
  await run(
    process.execPath,
    [
      pnpmCli,
      'install',
      '--prod',
      '--config.node-linker=hoisted',
      '--config.registry=https://registry.npmmirror.com/',
    ],
    { cwd: runtimeDirectory },
  );

  await cp(join(appDirectory, 'dist'), join(stagingDirectory, 'dist'), { recursive: true });
  const binDirectory = join(stagingDirectory, 'node_modules', '.bin');
  await mkdir(binDirectory, { recursive: true });
  if (process.platform === 'win32') {
    await writeFile(
      join(binDirectory, 'pnpm.CMD'),
      `@echo off\r\n"${process.execPath}" "${pnpmCli}" %*\r\n`,
      'utf8',
    );
  } else {
    const shimPath = join(binDirectory, 'pnpm');
    await writeFile(shimPath, `#!/bin/sh\nexec "${process.execPath}" "${pnpmCli}" "$@"\n`, 'utf8');
    await chmod(shimPath, 0o755);
  }
  const sourceManifest = JSON.parse(await readFile(join(appDirectory, 'package.json'), 'utf8'));
  await writeFile(
    join(stagingDirectory, 'package.json'),
    `${JSON.stringify(
      {
        name: sourceManifest.name,
        version: sourceManifest.version,
        description: sourceManifest.description,
        author: sourceManifest.author,
        private: true,
        type: 'module',
        main: sourceManifest.main,
      },
      null,
      2,
    )}\n`,
    'utf8',
  );
}

const env = {
  ...process.env,
  ELECTRON_MIRROR: process.env.ELECTRON_MIRROR ?? 'https://npmmirror.com/mirrors/electron/',
  ELECTRON_BUILDER_BINARIES_MIRROR:
    process.env.ELECTRON_BUILDER_BINARIES_MIRROR ??
    'https://npmmirror.com/mirrors/electron-builder-binaries/',
};

const stagingDirectory = await mkdtemp(join(tmpdir(), 'puff-app-'));
const runtimeDirectory = await mkdtemp(join(tmpdir(), 'puff-dsh-runtime-'));
try {
  await prepareStagingDirectory(stagingDirectory, runtimeDirectory);
  await run(
    process.execPath,
    [
      pnpmCli,
      'exec',
      'electron-builder',
      '--win',
      target,
      '--x64',
      '--projectDir',
      stagingDirectory,
      '--config',
      join(appDirectory, 'electron-builder.yml'),
      '--config.directories.output',
      outputDirectory,
    ],
    {
      cwd: appDirectory,
      env: { ...env, PUFF_RUNTIME_DIR: runtimeDirectory },
    },
  );
} finally {
  await Promise.all([
    rm(stagingDirectory, { recursive: true, force: true }),
    rm(runtimeDirectory, { recursive: true, force: true }),
  ]);
}
