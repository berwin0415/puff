import { delimiter } from 'node:path';
import { DSH_PROFILE_NAME } from './constants.js';

export type DshInvocationMode = 'initialize' | 'serve';

export interface BuildHostEnvironmentOptions {
  base?: NodeJS.ProcessEnv;
  pnpmShimDir?: string;
  extra?: NodeJS.ProcessEnv;
}

function pathKey(env: NodeJS.ProcessEnv): string {
  return Object.keys(env).find((key) => key.toLowerCase() === 'path') ?? 'PATH';
}

export function buildHostEnvironment(options: BuildHostEnvironmentOptions = {}): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = { ...(options.base ?? process.env), ...options.extra };
  env.ELECTRON_RUN_AS_NODE = '1';

  if (options.pnpmShimDir !== undefined && options.pnpmShimDir !== '') {
    const key = pathKey(env);
    env[key] = [options.pnpmShimDir, env[key]]
      .filter((part) => part !== undefined && part !== '')
      .join(delimiter);
  }

  return env;
}

export function buildDshInvocation(mode: DshInvocationMode, profile = DSH_PROFILE_NAME): string[] {
  if (mode === 'initialize') {
    return ['--profile', profile, '--from-default-profile', 'web', '--dump-config'];
  }

  return ['--profile', profile, '--no-open', '--port', '0'];
}
