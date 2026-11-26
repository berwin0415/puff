import { execFileSync } from 'node:child_process';
import { realpathSync, statSync } from 'node:fs';

import type { HostAdapter } from '@puff/agent-task-core';

function gitPath(cwd: string, flag: string): string | undefined {
  try {
    return realpathSync(
      execFileSync('git', ['-C', cwd, 'rev-parse', '--path-format=absolute', flag], {
        encoding: 'utf8',
        stdio: ['ignore', 'pipe', 'ignore'],
        timeout: 3000,
      }).trim(),
    );
  } catch {
    return undefined;
  }
}

export const codexAdapter: HostAdapter = {
  type: 'codex',
  version: '0.1',
  inspectWorkspace(workspace, context) {
    try {
      const root = realpathSync(workspace.rootPath);
      const cwd = realpathSync(context.cwd);
      if (!statSync(root).isDirectory() || !statSync(cwd).isDirectory()) return 'mismatch';
      const base = gitPath(root, '--show-toplevel');
      const current = gitPath(cwd, '--show-toplevel');
      if (cwd === root) {
        return workspace.execution.workspaceMode === 'worktree' ? 'preview-only' : 'match';
      }
      const common = gitPath(root, '--git-common-dir');
      if (
        workspace.execution.workspaceMode === 'worktree' &&
        base === root &&
        current === cwd &&
        current !== base &&
        common &&
        common === gitPath(cwd, '--git-common-dir')
      )
        return 'match';
      return 'mismatch';
    } catch {
      return 'mismatch';
    }
  },
  planPrompt(workspace) {
    return [
      '使用 $agent-task-automation:task-run 检查工作区 ' +
        JSON.stringify(workspace.id) +
        '，每次最多处理一项任务。',
      '以原生计划绑定的项目为执行工作区，每次重新读取该工作区当前配置。',
      '按任务来源、可用能力、工作目录、时间窗口、运行状态和授权范围检查后再执行。',
      '从宿主核对当前计划引用和工作区活动状态。活动状态无法确认时仅做只读检查。',
    ].join('\n');
  },
};
