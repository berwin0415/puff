import { randomUUID } from 'node:crypto';
import {
  closeSync,
  mkdirSync,
  openSync,
  readFileSync,
  renameSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { homedir } from 'node:os';
import { dirname, join } from 'node:path';

import {
  type PluginConfig,
  parseConfig,
  type RunSummary,
  workspaceIdPattern,
} from '@puff/agent-task-core';

export interface WorkspaceRunState {
  active?: { id: string; taskRef: string; startedAt: string; deliveryBinding?: string };
  pendingCheck?: boolean;
  last?: RunSummary & { recordedAt: string; deliveryBinding?: string };
}

function readJson(path: string): unknown {
  try {
    return JSON.parse(readFileSync(path, 'utf8')) as unknown;
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return undefined;
    throw error;
  }
}

function writeJson(path: string, value: unknown) {
  const temporary = `${path}.${randomUUID()}`;
  try {
    writeFileSync(temporary, `${JSON.stringify(value, null, 2)}\n`, { mode: 0o600, flag: 'wx' });
    renameSync(temporary, path);
  } finally {
    rmSync(temporary, { force: true });
  }
}

export function createStore(root = join(homedir(), '.puff', 'agent-task-automation')) {
  const configPath = join(root, 'config.json');

  function runPath(workspaceId: string) {
    if (!workspaceIdPattern.test(workspaceId)) throw new Error('无效的工作区 ID');
    return join(root, 'runs', `${workspaceId}.json`);
  }

  return {
    root,
    readConfig: () => parseConfig(readJson(configPath) ?? { schemaVersion: 1, workspaces: [] }),
    writeConfig: (config: PluginConfig) => writeJson(configPath, parseConfig(config)),
    readRun: (id: string) => (readJson(runPath(id)) ?? {}) as WorkspaceRunState,
    writeRun(id: string, value: WorkspaceRunState) {
      const path = runPath(id);
      mkdirSync(dirname(path), { recursive: true, mode: 0o700 });
      writeJson(path, value);
    },
    update<T>(operation: () => T): T {
      mkdirSync(root, { recursive: true, mode: 0o700 });
      const path = join(root, 'write.lock');
      // shortcut: 异常退出留下的短事务锁需要核对后清理，有可验证的恢复需求时再增加自动清理。
      const fd = openSync(path, 'wx', 0o600);
      try {
        return operation();
      } finally {
        closeSync(fd);
        rmSync(path);
      }
    },
  };
}
export type Store = ReturnType<typeof createStore>;
