import { EventEmitter } from 'node:events';
import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { PassThrough } from 'node:stream';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { getRestartDecision, type HostChildProcess, HostSupervisor } from './host-supervisor.js';

class FakeChild extends EventEmitter {
  stdout = new PassThrough();
  stderr = new PassThrough();
  exitCode: number | null = null;
  signalCode: NodeJS.Signals | null = null;
  pid = 1234;

  kill(): boolean {
    this.exitCode = 0;
    this.emit('exit', 0, null);
    return true;
  }
}

const cleanupPaths: string[] = [];

afterEach(async () => {
  await Promise.all(
    cleanupPaths.splice(0).map((path) => rm(path, { recursive: true, force: true })),
  );
});

describe('getRestartDecision', () => {
  it('uses the fixed 1s/2s/4s backoff and stops after three retries', () => {
    const delays = [1_000, 2_000, 4_000];

    expect(getRestartDecision(0, delays)).toEqual({ delayMs: 1_000, nextAttempt: 1 });
    expect(getRestartDecision(1, delays)).toEqual({ delayMs: 2_000, nextAttempt: 2 });
    expect(getRestartDecision(2, delays)).toEqual({ delayMs: 4_000, nextAttempt: 3 });
    expect(getRestartDecision(3, delays)).toBeNull();
  });
});

describe('HostSupervisor', () => {
  it('emits a redacted ready event for the loopback URL', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-supervisor-'));
    cleanupPaths.push(directory);
    const child = new FakeChild();
    const spawnHost = vi.fn(() => child as unknown as HostChildProcess);
    const supervisor = new HostSupervisor({
      nodeExecutable: process.execPath,
      dshEntry: 'dsh-entry.js',
      workspaceDir: directory,
      logDir: directory,
      env: {},
      spawnHost,
    });
    const ready = new Promise<{ url: string; redactedUrl: string }>((resolve) => {
      supervisor.once('ready', resolve);
    });

    await supervisor.start();
    child.stdout.write('dsh web: http://127.0.0.1:41234/?token=secret-token\n');

    await expect(ready).resolves.toEqual({
      url: 'http://127.0.0.1:41234/?token=secret-token',
      origin: 'http://127.0.0.1:41234',
      port: 41234,
      redactedUrl: 'http://127.0.0.1:41234/',
    });
    expect(spawnHost).toHaveBeenCalledOnce();
    await supervisor.stop();
  });

  it('restarts after an unexpected exit', async () => {
    const directory = await mkdtemp(join(tmpdir(), 'puff-supervisor-'));
    cleanupPaths.push(directory);
    const first = new FakeChild();
    const second = new FakeChild();
    const children = [first, second];
    const spawnHost = vi.fn(() => children.shift() as unknown as HostChildProcess);
    const supervisor = new HostSupervisor({
      nodeExecutable: process.execPath,
      dshEntry: 'dsh-entry.js',
      workspaceDir: directory,
      logDir: directory,
      env: {},
      spawnHost,
      restartDelaysMs: [10],
    });
    const restarting = new Promise<void>((resolve) => {
      supervisor.once('restarting', () => resolve());
    });

    await supervisor.start();
    first.exitCode = 1;
    first.emit('exit', 1, null);
    await restarting;
    await new Promise((resolve) => setTimeout(resolve, 25));

    expect(spawnHost).toHaveBeenCalledTimes(2);
    await supervisor.stop();
  });
});
