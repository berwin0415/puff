import { join, resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  dshProfileDir,
  hasDshProfile,
  resolveDefaultWorkspaceDir,
  resolveDshHome,
} from './dsh-paths.js';

const absoluteDshHome = process.platform === 'win32' ? 'D:\\dsh-home' : '/dsh-home';
const userHome = process.platform === 'win32' ? 'C:\\Users\\puff' : '/Users/puff';

describe('DSH paths', () => {
  it('honors an explicit DSH_HOME', () => {
    expect(resolveDshHome({ env: { DSH_HOME: absoluteDshHome } })).toBe(resolve(absoluteDshHome));
  });

  it('reads DSH_HOME from the process environment by default', () => {
    const previous = process.env.DSH_HOME;
    process.env.DSH_HOME = absoluteDshHome;
    try {
      expect(resolveDshHome()).toBe(resolve(absoluteDshHome));
    } finally {
      if (previous === undefined) {
        delete process.env.DSH_HOME;
      } else {
        process.env.DSH_HOME = previous;
      }
    }
  });

  it('uses the user home by default', () => {
    expect(resolveDshHome({ env: {}, homeDir: userHome })).toBe(join(userHome, '.dsh'));
  });

  it('builds the puff profile directory', () => {
    expect(dshProfileDir(absoluteDshHome)).toBe(join(absoluteDshHome, 'profiles', 'puff'));
  });

  it('reports a missing profile', () => {
    expect(hasDshProfile(join('__missing-profile-home__', 'dsh'))).toBe(false);
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
