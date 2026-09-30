import { describe, expect, it } from 'vitest';
import {
  dshProfileDir,
  hasDshProfile,
  resolveDefaultWorkspaceDir,
  resolveDshHome,
} from './dsh-paths.js';

describe('DSH paths', () => {
  it('honors an explicit DSH_HOME', () => {
    expect(resolveDshHome({ env: { DSH_HOME: 'D:\\dsh-home' } })).toBe('D:\\dsh-home');
  });

  it('reads DSH_HOME from the process environment by default', () => {
    const previous = process.env.DSH_HOME;
    process.env.DSH_HOME = 'D:\\process-dsh-home';
    try {
      expect(resolveDshHome()).toBe('D:\\process-dsh-home');
    } finally {
      if (previous === undefined) {
        delete process.env.DSH_HOME;
      } else {
        process.env.DSH_HOME = previous;
      }
    }
  });

  it('uses the user home by default', () => {
    expect(resolveDshHome({ env: {}, homeDir: 'C:\\Users\\puff' })).toBe('C:\\Users\\puff\\.dsh');
  });

  it('builds the puff profile directory', () => {
    expect(dshProfileDir('C:\\Users\\puff\\.dsh')).toBe('C:\\Users\\puff\\.dsh\\profiles\\puff');
  });

  it('reports a missing profile', () => {
    expect(hasDshProfile('C:\\missing-profile-home')).toBe(false);
  });

  it('uses LOCALAPPDATA for the default workspace on Windows', () => {
    expect(
      resolveDefaultWorkspaceDir({
        platform: 'win32',
        homeDir: 'C:\\Users\\puff',
        env: { LOCALAPPDATA: 'C:\\Users\\puff\\AppData\\Local' },
      }),
    ).toBe('C:\\Users\\puff\\AppData\\Local\\puff\\workspace');
  });
});
