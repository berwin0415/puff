import { describe, expect, it } from 'vitest';
import { buildDshInvocation, buildHostEnvironment } from './startup-environment.js';

describe('buildHostEnvironment', () => {
  it('runs Electron as Node and prepends the pnpm shim', () => {
    const env = buildHostEnvironment({
      base: { Path: 'C:\\Windows\\System32' },
      pnpmShimDir: 'C:\\Users\\puff\\bin',
      extra: { PUFF_PNPM_NODE: 'puff.exe' },
    });

    expect(env.ELECTRON_RUN_AS_NODE).toBe('1');
    expect(env.Path).toBe('C:\\Users\\puff\\bin;C:\\Windows\\System32');
    expect(env.PUFF_PNPM_NODE).toBe('puff.exe');
  });
});

describe('buildDshInvocation', () => {
  it('initializes the independent web-derived profile', () => {
    expect(buildDshInvocation('initialize')).toEqual([
      '--profile',
      'puff',
      '--from-default-profile',
      'web',
      '--dump-config',
    ]);
  });

  it('serves the profile on an OS-assigned loopback port', () => {
    expect(buildDshInvocation('serve')).toEqual(['--profile', 'puff', '--no-open', '--port', '0']);
  });
});
