import { mkdtemp, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { ensureDshProfile } from '../src/main/dsh-profile.js';
import { resolveDshEntry, resolvePnpmEntry } from '../src/main/dsh-runtime.js';
import { type HostReadyEvent, HostSupervisor } from '../src/main/host-supervisor.js';
import { ensurePnpmShim } from '../src/main/pnpm-shim.js';
import { buildHostEnvironment } from '../src/main/startup-environment.js';

describe('real DSH host', () => {
  let tempDirectory = '';
  let supervisor: HostSupervisor | null = null;

  beforeAll(async () => {
    tempDirectory = await mkdtemp(join(tmpdir(), 'puff-dsh-e2e-'));
  }, 60_000);

  afterAll(async () => {
    await supervisor?.stop();
    if (tempDirectory !== '') {
      await rm(tempDirectory, { recursive: true, force: true });
    }
  }, 60_000);

  it('initializes the puff profile, serves an authenticated UI, and stops cleanly', async () => {
    const workspaceDir = join(tempDirectory, 'workspace');
    const dshHome = join(tempDirectory, 'dsh-home');
    const logDir = join(tempDirectory, 'logs');
    const nodeExecutable = process.execPath;
    const dshEntry = resolveDshEntry();
    const pnpmEntry = resolvePnpmEntry();
    const pnpmShimDir = await ensurePnpmShim({
      userDataDir: tempDirectory,
      nodeExecutable,
      pnpmEntry,
    });
    const env = buildHostEnvironment({
      base: { ...process.env, DSH_HOME: dshHome },
      pnpmShimDir,
      extra: {
        PUFF_PNPM_NODE: nodeExecutable,
        PUFF_PNPM_ENTRY: pnpmEntry,
      },
    });

    await ensureDshProfile({
      nodeExecutable,
      dshEntry,
      dshHome,
      workspaceDir,
      env,
    });

    supervisor = new HostSupervisor({
      nodeExecutable,
      dshEntry,
      workspaceDir,
      logDir,
      env,
    });

    const ready = new Promise<HostReadyEvent>((resolve) => {
      supervisor?.once('ready', resolve);
    });
    await supervisor.start();

    const readyEvent = await ready;
    let response = await fetch(readyEvent.url, { redirect: 'manual' });
    if (response.status >= 300 && response.status < 400) {
      const location = response.headers.get('location');
      const cookie = response.headers.get('set-cookie')?.split(';')[0];
      expect(location).not.toBeNull();
      expect(cookie).toBeTruthy();
      response = await fetch(new URL(location ?? '', readyEvent.url), {
        headers: cookie === undefined ? {} : { cookie },
      });
    }
    expect(response.status).toBe(200);
    expect(readyEvent.redactedUrl).toBe(`http://127.0.0.1:${String(readyEvent.port)}/`);

    await supervisor.stop();
    supervisor = null;
  }, 180_000);
});
