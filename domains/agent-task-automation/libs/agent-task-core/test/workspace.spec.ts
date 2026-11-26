import { describe, expect, it } from 'vitest';

import { parseConfig } from '../src/config.js';
import { checkOperation, insideWindow, prepareRun } from '../src/execution-context.js';
import type { HostAdapter, HostRunContext } from '../src/host-contract.js';

const host: HostAdapter = {
  type: 'test',
  version: '1',
  inspectWorkspace: () => 'match',
  planPrompt: () => '',
};
const run: HostRunContext = {
  trigger: 'manual',
  cwd: '/test',
  activity: 'idle',
  availableRefs: ['task-skill', 'delivery-tool'],
};
const now = new Date('2026-11-26T01:30:00Z');
const input = {
  id: 'test',
  rootPath: '/test',
  host: { type: 'test', adapterVersion: '1' },
  execution: {
    maxActiveRuns: 1,
    workspaceMode: 'local',
    taskScope: '范围',
    allowedPaths: ['docs'],
    allowedActions: ['list', 'read', 'modify', 'verify', 'publish'],
    timezone: 'Asia/Shanghai',
    windows: [{ days: [4], startMinute: 540, endMinute: 1080 }],
  },
  capabilities: [
    {
      role: 'task-source',
      ref: 'task-skill',
      kind: 'skill',
      operations: ['list', 'read'],
      enabled: true,
    },
    {
      role: 'delivery',
      ref: 'delivery-tool',
      kind: 'mcp',
      operations: ['publish', 'observe'],
      enabled: true,
    },
  ],
  schedule: { intervalMinutes: 30, hostPlanRef: 'test-plan' },
};
const config = (workspace: unknown = input) =>
  parseConfig({ schemaVersion: 1, workspaces: [workspace] }).workspaces[0];

describe('工作区配置与触发', () => {
  it('默认关闭计划，拒绝错误的路径、角色操作和多活动名额', () => {
    expect(config().execution.automaticEnabled).toBe(false);
    expect(() => config({ ...input, rootPath: '../test' })).toThrow('绝对路径');
    expect(() =>
      config({ ...input, capabilities: [...input.capabilities, input.capabilities[0]] }),
    ).toThrow('一个绑定');
    expect(() =>
      config({ ...input, execution: { ...input.execution, allowedPaths: ['../outside'] } }),
    ).toThrow('相对于');
    expect(() => config({ ...input, execution: { ...input.execution, maxActiveRuns: 2 } })).toThrow(
      '活动名额',
    );
  });

  it('按配置时区解释窗口起点、终点和跨日窗口', () => {
    expect(insideWindow(config(), new Date('2026-11-26T01:00:00Z'))).toBe(true);
    expect(insideWindow(config(), new Date('2026-11-26T10:00:00Z'))).toBe(false);
    const workspace = config({
      ...input,
      execution: {
        ...input.execution,
        windows: [{ days: [4], startMinute: 1380, endMinute: 120 }],
      },
    });
    expect(insideWindow(workspace, new Date('2026-11-26T17:00:00Z'))).toBe(true);
    expect(insideWindow(workspace, new Date('2026-11-26T18:00:00Z'))).toBe(false);
  });

  it('共享手动与计划门槛，计划需要启用和正确引用', () => {
    const workspace = config();
    const scheduled = { ...run, trigger: 'scheduled' as const, hostPlanRef: 'test-plan' };
    expect(prepareRun(workspace, host, run, now).mode).toBe('execute');
    expect(prepareRun(workspace, host, scheduled, now)).toMatchObject({
      reason: 'plan-disabled-or-mismatched',
    });
    workspace.execution.automaticEnabled = true;
    expect(prepareRun(workspace, host, { ...scheduled, hostPlanRef: 'wrong' }, now).mode).toBe(
      'blocked',
    );
    expect(prepareRun(workspace, host, scheduled, now).mode).toBe('execute');
    workspace.execution.paused = true;
    expect(prepareRun(workspace, host, run, now)).toMatchObject({ reason: 'paused' });
    expect(prepareRun(workspace, host, { ...run, trigger: 'preview' }, now).mode).toBe('read-only');
  });

  it('无法确认活动状态时只返回已授权的读取操作', () => {
    const workspace = config();
    expect(checkOperation(workspace, run.availableRefs, 'delivery', 'observe')).toBe(false);
    const decision = prepareRun(workspace, host, { ...run, activity: 'unknown' }, now);
    expect(decision).toMatchObject({
      mode: 'read-only',
      reason: 'host-activity-unknown',
      context: { allowedActions: ['list', 'read'], allowedPaths: [] },
    });
    expect(prepareRun(workspace, host, { ...run, availableRefs: [] }, now)).toMatchObject({
      reason: 'missing-task-source',
    });
  });
});
