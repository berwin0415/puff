import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, resolve } from 'node:path';
import { DSH_PROFILE_NAME } from './constants.js';

export interface DshPathOptions {
  env?: NodeJS.ProcessEnv;
  homeDir?: string;
  platform?: NodeJS.Platform;
}

function selectedHomeDir(options: DshPathOptions): string {
  return options.homeDir ?? homedir();
}

export function resolveDshHome(options: DshPathOptions = {}): string {
  const configured = (options.env ?? process.env).DSH_HOME?.trim();
  return configured === undefined || configured === ''
    ? join(selectedHomeDir(options), '.dsh')
    : resolve(configured);
}

export function dshProfileDir(dshHome: string, profile = DSH_PROFILE_NAME): string {
  return join(dshHome, 'profiles', profile);
}

export function hasDshProfile(dshHome: string, profile = DSH_PROFILE_NAME): boolean {
  return existsSync(join(dshProfileDir(dshHome, profile), 'package.json'));
}

export function hasDshProfileDirectory(dshHome: string, profile = DSH_PROFILE_NAME): boolean {
  return existsSync(dshProfileDir(dshHome, profile));
}

export function resolveDefaultWorkspaceDir(options: DshPathOptions = {}): string {
  const env = options.env ?? process.env;
  const platform = options.platform ?? process.platform;
  const home = selectedHomeDir(options);

  if (platform === 'win32') {
    const localAppData = env.LOCALAPPDATA?.trim();
    return join(
      localAppData === undefined || localAppData === '' ? home : localAppData,
      'puff',
      'workspace',
    );
  }

  if (platform === 'darwin') {
    return join(home, 'Library', 'Application Support', 'puff', 'workspace');
  }

  const xdgDataHome = env.XDG_DATA_HOME?.trim();
  return join(
    xdgDataHome === undefined || xdgDataHome === '' ? join(home, '.local', 'share') : xdgDataHome,
    'puff',
    'workspace',
  );
}
