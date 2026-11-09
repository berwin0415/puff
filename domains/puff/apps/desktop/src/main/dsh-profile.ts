import { spawn } from 'node:child_process';
import { mkdir } from 'node:fs/promises';
import { dshProfileDir, hasDshProfile, hasDshProfileDirectory } from './dsh-paths.js';
import { redactHostText } from './logger.js';
import { buildDshInvocation } from './startup-environment.js';

export interface EnsureDshProfileOptions {
  nodeExecutable: string;
  dshEntry: string;
  dshHome: string;
  workspaceDir: string;
  env: NodeJS.ProcessEnv;
  profile?: string;
  timeoutMs?: number;
}

function appendLimited(current: string, chunk: string, limit = 64 * 1024): string {
  const next = current + chunk;
  return next.length > limit ? next.slice(next.length - limit) : next;
}

export async function ensureDshProfile(options: EnsureDshProfileOptions): Promise<void> {
  const profile = options.profile ?? 'puff';
  if (hasDshProfile(options.dshHome, profile)) {
    return;
  }

  if (hasDshProfileDirectory(options.dshHome, profile)) {
    throw new Error(
      `DSH profile directory exists but is incomplete: ${dshProfileDir(options.dshHome, profile)}`,
    );
  }

  await mkdir(options.workspaceDir, { recursive: true });

  await new Promise<void>((resolve, reject) => {
    const child = spawn(
      options.nodeExecutable,
      [options.dshEntry, ...buildDshInvocation('initialize', profile)],
      {
        cwd: options.workspaceDir,
        env: options.env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
      },
    );

    let stdout = '';
    let stderr = '';
    let settled = false;

    const settle = (error?: Error): void => {
      if (settled) {
        return;
      }
      settled = true;
      clearTimeout(timeout);
      if (error === undefined) {
        resolve();
      } else {
        reject(error);
      }
    };

    const timeout = setTimeout(() => {
      child.kill();
      settle(new Error(`Timed out while initializing DSH profile "${profile}"`));
    }, options.timeoutMs ?? 120_000);

    child.stdout.setEncoding('utf8');
    child.stderr.setEncoding('utf8');
    child.stdout.on('data', (chunk: string) => {
      stdout = appendLimited(stdout, chunk);
    });
    child.stderr.on('data', (chunk: string) => {
      stderr = appendLimited(stderr, chunk);
    });
    child.once('error', (error) => {
      settle(new Error(`Failed to initialize DSH profile: ${error.message}`));
    });
    child.once('exit', (code, signal) => {
      if (code === 0) {
        settle();
        return;
      }

      const detail = redactHostText(stderr.trim() || stdout.trim());
      settle(
        new Error(
          `DSH profile initialization failed with ${signal === null ? `exit code ${String(code)}` : `signal ${signal}`}${
            detail === '' ? '' : `: ${detail}`
          }`,
        ),
      );
    });
  });
}
