import { existsSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, posix, resolve, win32 } from 'node:path';
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
  // 使用被模拟平台的拼接规则，让测试和调用方可以在任意宿主上解析 Windows 路径。
  const pathJoin = platform === 'win32' ? win32.join : posix.join;

  if (platform === 'win32') {
    const localAppData = env.LOCALAPPDATA?.trim();
    return pathJoin(
      localAppData === undefined || localAppData === '' ? home : localAppData,
      'puff',
      'workspace',
    );
  }

  if (platform === 'darwin') {
    return pathJoin(home, 'Library', 'Application Support', 'puff', 'workspace');
  }

  const xdgDataHome = env.XDG_DATA_HOME?.trim();
  return pathJoin(
    xdgDataHome === undefined || xdgDataHome === ''
      ? pathJoin(home, '.local', 'share')
      : xdgDataHome,
    'puff',
    'workspace',
  );
}
