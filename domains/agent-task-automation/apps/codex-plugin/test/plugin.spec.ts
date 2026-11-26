import { execFileSync, spawnSync } from 'node:child_process';
import { cpSync, existsSync, mkdtempSync, readFileSync, realpathSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { afterEach, beforeEach, expect, it } from 'vitest';

import { codexAdapter } from '../src/adapter.js';
import { runCommand } from '../src/commands.js';
import { createStore } from '../src/store.js';

const plugin = fileURLToPath(new URL('../dist/plugin/', import.meta.url));
const repo = fileURLToPath(new URL('../../../../../', import.meta.url));
const now = new Date('2026-11-26T01:30:00Z');
let temp: string;

beforeEach(() => {
  temp = realpathSync(mkdtempSync(join(tmpdir(), 'puff-automation-')));
});
afterEach(() => {
  rmSync(temp, { recursive: true, force: true });
});

function git(cwd: string, ...args: string[]) {
  execFileSync('git', args, { cwd, stdio: 'ignore' });
}

const draft = {
  id: 'puff-test',
  rootPath: repo,
  host: { type: 'codex', adapterVersion: '0.1' },
  execution: {
    automaticEnabled: false,
    maxActiveRuns: 1,
    workspaceMode: 'local' as const,
    taskScope: '文档',
    allowedPaths: ['docs'],
    allowedActions: ['list', 'read', 'verify', 'modify', 'publish', 'observe'],
    timezone: 'Asia/Shanghai',
    windows: [{ days: [4], startMinute: 540, endMinute: 1080 }],
  },
  capabilities: [
    {
      role: 'task-source' as const,
      kind: 'skill' as const,
      ref: 'selected-task',
      operations: ['read', 'list'],
      enabled: true,
    },
    {
      role: 'delivery' as const,
      kind: 'skill' as const,
      ref: 'selected-delivery',
      operations: ['publish', 'observe'],
      enabled: true,
    },
  ],
  schedule: { intervalMinutes: 30 },
};

it('识别 Git worktree，拒绝其他仓库或本地模式的替代目录', () => {
  git(temp, 'init');
  git(
    temp,
    '-c',
    'user.name=Test',
    '-c',
    'user.email=test@example.invalid',
    '-c',
    'commit.gpgsign=false',
    'commit',
    '--allow-empty',
    '-m',
    'base',
  );
  const linked = `${temp}-linked`;
  try {
    git(temp, 'worktree', 'add', '--detach', linked);
    const workspace = {
      ...draft,
      rootPath: temp,
      execution: {
        ...draft.execution,
        maxActiveRuns: 1 as const,
        workspaceMode: 'worktree' as const,
      },
    };
    const context = {
      trigger: 'manual' as const,
      cwd: linked,
      availableRefs: [],
      activity: 'unknown' as const,
    };
    expect(codexAdapter.inspectWorkspace(workspace, context)).toBe('match');
    expect(codexAdapter.inspectWorkspace(workspace, { ...context, cwd: temp })).toBe(
      'preview-only',
    );
    expect(codexAdapter.inspectWorkspace(workspace, { ...context, cwd: repo })).toBe('mismatch');
    expect(
      codexAdapter.inspectWorkspace(
        { ...workspace, execution: { ...workspace.execution, workspaceMode: 'local' } },
        context,
      ),
    ).toBe('mismatch');
  } finally {
    rmSync(linked, { recursive: true, force: true });
  }
});

it('同一工作区重叠触发共享活动名额，待核对的交付只能用原能力核对', () => {
  const cwd = process.cwd();
  process.chdir(repo);
  try {
    const store = createStore(join(temp, 'storage'));
    const call = (command: string, input: unknown) => runCommand(store, command, input, now);
    call('configure', draft);
    const run = {
      workspaceId: draft.id,
      trigger: 'manual',
      activity: 'idle',
      availableRefs: ['selected-task', 'selected-delivery'],
      taskRef: 'sample-task',
    };
    expect(call('prepare', { ...run, activity: 'unknown' })).toMatchObject({ mode: 'read-only' });
    expect(call('plan', run)).toMatchObject({
      prompt: expect.stringContaining('$agent-task-automation:task-run'),
    });
    call('attach-plan', { workspaceId: draft.id, hostPlanRef: 'plan' });
    const { runId } = call('start', run) as { runId: string };
    expect(runId).toBeTruthy();
    expect(call('start', { ...run, trigger: 'scheduled', hostPlanRef: 'plan' })).toMatchObject({
      reason: 'workspace-busy',
    });
    expect(call('status', run)).toMatchObject({ pendingCheck: true });
    expect(
      call('check-action', { ...run, runId, action: 'modify', paths: ['docs/example.md'] }),
    ).toEqual({ allowed: true });
    expect(call('check-action', { ...run, runId, action: 'modify', paths: ['README.md'] })).toEqual(
      { allowed: false },
    );
    call('finish', { ...run, runId, outcome: 'unknown-delivery' });
    expect(call('start', run)).toMatchObject({ mode: 'read-only', reason: 'unknown-delivery' });
    expect(() =>
      call('reconcile', { ...run, availableRefs: ['selected-task'], deliveryRef: 'result' }),
    ).toThrow('原交付能力');
    expect(() => call('reconcile', { ...run, deliveryAbsent: true })).toThrow('依据');
    expect(
      call('reconcile', { ...run, deliveryAbsent: true, detail: '原能力确认未提交' }),
    ).toMatchObject({ outcome: 'failed' });
    const retry = call('start', run) as { runId: string };
    expect(retry.runId).toBeTruthy();
    call('finish', { ...run, runId: retry.runId, outcome: 'unknown-delivery' });
    call('reconcile', { ...run, deliveryRef: 'result' });
    expect(call('start', run)).toMatchObject({ reason: 'task-already-completed' });
    call('pause', run);
    expect(call('prepare', run)).toMatchObject({ reason: 'paused' });
    expect(call('prepare', { ...run, trigger: 'preview' })).toMatchObject({ mode: 'read-only' });
    call('resume', run);
    expect(call('prepare', run)).toMatchObject({ mode: 'execute' });
  } finally {
    process.chdir(cwd);
  }
});

it('保留多个工作区并允许每个工作区使用独立配置和活动名额', () => {
  const cwd = process.cwd();
  process.chdir(repo);
  try {
    const store = createStore(join(temp, 'storage'));
    const call = (command: string, input: unknown) => runCommand(store, command, input, now);
    const second = {
      ...draft,
      id: 'puff-second',
      rootPath: temp,
      execution: {
        ...draft.execution,
        taskScope: '第二个工作区',
        allowedPaths: ['.'],
      },
      capabilities: draft.capabilities.map((binding) => ({
        ...binding,
        ref: `${binding.ref}-second`,
      })),
      schedule: { intervalMinutes: 15 },
    };
    call('configure', draft);
    call('configure', second);
    expect(call('list', {})).toEqual([
      expect.objectContaining({ id: draft.id, rootPath: realpathSync(repo), active: false }),
      expect.objectContaining({ id: second.id, rootPath: temp, active: false }),
    ]);
    expect(call('config', {})).toMatchObject({
      workspaces: [
        expect.objectContaining({
          id: draft.id,
          execution: expect.objectContaining({ taskScope: '文档' }),
        }),
        expect.objectContaining({
          id: second.id,
          execution: expect.objectContaining({ taskScope: '第二个工作区' }),
          capabilities: expect.arrayContaining([
            expect.objectContaining({ ref: 'selected-task-second' }),
          ]),
        }),
      ],
    });
    const firstRun = call('start', {
      workspaceId: draft.id,
      trigger: 'manual',
      activity: 'idle',
      availableRefs: ['selected-task', 'selected-delivery'],
      taskRef: 'first-task',
    }) as { runId: string };
    process.chdir(temp);
    const secondRun = call('start', {
      workspaceId: second.id,
      trigger: 'manual',
      activity: 'idle',
      availableRefs: ['selected-task-second', 'selected-delivery-second'],
      taskRef: 'second-task',
    }) as { runId: string };
    expect(firstRun.runId).toBeTruthy();
    expect(secondRun.runId).toBeTruthy();
    expect(call('status', { workspaceId: draft.id })).toMatchObject({
      active: { taskRef: 'first-task' },
    });
    expect(call('status', { workspaceId: second.id })).toMatchObject({
      active: { taskRef: 'second-task' },
    });
  } finally {
    process.chdir(cwd);
  }
});

it('可搬迁产物包含 manifest、两个 skill 所引用的资源和独立配置命令', () => {
  const installed = join(temp, 'plugin');
  cpSync(plugin, installed, { recursive: true });
  const manifest = JSON.parse(readFileSync(join(installed, 'plugin.json'), 'utf8'));
  const onboarding = manifest.extensions['com.openai'].onboardingSkill;
  for (const location of [onboarding, 'skills/task-run/SKILL.md']) {
    const file = resolve(installed, location);
    const skill = readFileSync(file, 'utf8');
    expect(skill).toMatch(/^---\nname: [\w-]+\ndescription: /);
    for (const match of skill.matchAll(/\]\(([^)]+)\)/g)) {
      expect(existsSync(resolve(dirname(file), match[1]))).toBe(true);
    }
  }
  const input = JSON.stringify({ schemaVersion: 1, workspaces: [draft] });
  const result = spawnSync(
    process.execPath,
    [join(installed, 'scripts/automation.mjs'), 'validate', '-'],
    {
      cwd: temp,
      input,
      encoding: 'utf8',
    },
  );
  expect(result.stderr).toBe('');
  expect(result.status).toBe(0);
  expect(JSON.parse(result.stdout).workspaces[0].id).toBe(draft.id);
});
