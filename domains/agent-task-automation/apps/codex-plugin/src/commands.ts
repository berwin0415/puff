import { randomUUID } from 'node:crypto';
import { lstatSync, realpathSync } from 'node:fs';
import { isAbsolute, relative, resolve, sep } from 'node:path';

import {
  type CapabilityRole,
  capabilityRoles,
  checkOperation,
  type HostRunContext,
  parseConfig,
  prepareRun,
  type RunSummary,
  type WorkspaceBinding,
} from '@puff/agent-task-core';

import { codexAdapter } from './adapter.js';
import type { Store } from './store.js';

function string(value: unknown, label: string) {
  if (typeof value !== 'string' || !value.trim() || value.length > 2048)
    throw new Error(`缺少或无效的${label}`);
  return value;
}

function optional(value: unknown, label: string) {
  return value === undefined ? undefined : string(value, label);
}

function inputRecord(input: unknown): Record<string, unknown> {
  if (input == null) return {};
  if (typeof input !== 'object' || Array.isArray(input))
    throw new Error('命令输入必须是 JSON 对象');
  return input as Record<string, unknown>;
}

function runContext(input: Record<string, unknown>): HostRunContext {
  const { trigger = 'preview', activity = 'unknown', availableRefs = [] } = input;
  if (trigger !== 'manual' && trigger !== 'scheduled' && trigger !== 'preview')
    throw new Error('未知的触发方式');
  if (activity !== 'idle' && activity !== 'busy' && activity !== 'unknown')
    throw new Error('未知的宿主活动状态');
  if (!Array.isArray(availableRefs) || availableRefs.some((ref) => typeof ref !== 'string')) {
    throw new Error('能力引用必须为字符串数组');
  }
  return {
    trigger,
    activity,
    availableRefs,
    cwd: process.cwd(),
    hostPlanRef: optional(input.hostPlanRef, '计划引用'),
  };
}

function deliveryBinding(workspace: WorkspaceBinding) {
  const binding = workspace.capabilities.find((item) => item.enabled && item.role === 'delivery');
  return binding ? JSON.stringify([binding.kind, binding.ref]) : undefined;
}

function summary(input: Record<string, unknown>): RunSummary {
  const outcome = input.outcome;
  if (
    outcome !== 'completed' &&
    outcome !== 'blocked' &&
    outcome !== 'failed' &&
    outcome !== 'cancelled' &&
    outcome !== 'unknown-delivery'
  )
    throw new Error('无效的运行结果');
  return {
    outcome,
    taskRef: optional(input.taskRef, '任务引用'),
    verification: optional(input.verification, '验证摘要'),
    deliveryRef: optional(input.deliveryRef, '交付引用'),
    detail: optional(input.detail, '结果摘要'),
  };
}

function withPathScope(workspace: WorkspaceBinding, paths: unknown) {
  if (!Array.isArray(paths) || !paths.length) return false;
  const cwd = realpathSync(process.cwd());
  return paths.every((path) => {
    if (
      typeof path !== 'string' ||
      isAbsolute(path) ||
      /^[A-Za-z]:/.test(path) ||
      path.includes('\\') ||
      path.split('/').some((part) => !part || part === '..' || part === '.')
    )
      return false;
    const candidate = resolve(cwd, path);
    let existing = candidate;
    for (;;) {
      try {
        const canonical = realpathSync(existing);
        if (canonical !== existing) return false;
        break;
      } catch (error) {
        if ((error as NodeJS.ErrnoException).code !== 'ENOENT') return false;
        if (lstatSync(existing, { throwIfNoEntry: false })?.isSymbolicLink()) return false;
        const parent = resolve(existing, '..');
        if (parent === existing) return false;
        existing = parent;
      }
    }
    return workspace.execution.allowedPaths.some((root) => {
      const offset = relative(resolve(cwd, root), candidate);
      return offset !== '..' && !offset.startsWith(`..${sep}`) && !isAbsolute(offset);
    });
  });
}

export function runCommand(
  store: Store,
  command: string,
  payload: unknown,
  now = new Date(),
): unknown {
  const input = inputRecord(payload);
  if (command === 'validate') return parseConfig(input);
  if (command === 'config') return store.readConfig();

  const execute = () => {
    const config = store.readConfig();
    if (command === 'configure') {
      const [draft] = parseConfig({ schemaVersion: 1, workspaces: [input] }).workspaces;
      if (
        !draft ||
        draft.host.type !== codexAdapter.type ||
        draft.host.adapterVersion !== codexAdapter.version
      ) {
        throw new Error('当前版本只接受 Codex 工作区绑定');
      }
      draft.rootPath = realpathSync(draft.rootPath);
      if (
        codexAdapter.inspectWorkspace(draft, {
          cwd: draft.rootPath,
          trigger: 'preview',
          activity: 'unknown',
          availableRefs: [],
        }) === 'mismatch'
      ) {
        throw new Error('工作区目录不可用');
      }
      if (
        config.workspaces.some(({ id, rootPath }) => id !== draft.id && rootPath === draft.rootPath)
      ) {
        throw new Error('当前目录已经绑定到另一个工作区');
      }
      if (store.readRun(draft.id).active) throw new Error('工作区存在活动任务');
      const current = config.workspaces.find(({ id }) => id === draft.id);
      if (current && current.rootPath !== draft.rootPath)
        throw new Error('新目录需要独立的工作区 ID');
      draft.execution.automaticEnabled = false;
      if (draft.schedule) draft.schedule.hostPlanRef = current?.schedule?.hostPlanRef;
      store.writeConfig({
        ...config,
        workspaces: [...config.workspaces.filter(({ id }) => id !== draft.id), draft],
      });
      return draft;
    }

    if (command === 'list') {
      return config.workspaces.map((workspace) => {
        const state = store.readRun(workspace.id);
        return {
          id: workspace.id,
          rootPath: workspace.rootPath,
          host: workspace.host,
          automaticEnabled: workspace.execution.automaticEnabled,
          paused: workspace.execution.paused ?? false,
          schedule: workspace.schedule,
          active: Boolean(state.active),
          lastOutcome: state.last?.outcome,
        };
      });
    }

    const id = string(input.workspaceId, '工作区 ID');
    const workspace = config.workspaces.find((item) => item.id === id);
    if (!workspace) throw new Error('工作区尚未绑定');
    const state = store.readRun(id);

    if (command === 'status') return { workspace, ...state };
    if (command === 'plan')
      return {
        workspaceId: id,
        rootPath: workspace.rootPath,
        workspaceMode: workspace.execution.workspaceMode,
        timezone: workspace.execution.timezone,
        windows: workspace.execution.windows,
        schedule: workspace.schedule,
        prompt: codexAdapter.planPrompt(workspace),
      };
    if (command === 'attach-plan') {
      if (!workspace.schedule) throw new Error('请先配置检查间隔');
      workspace.schedule.hostPlanRef = string(input.hostPlanRef, '宿主计划引用');
      workspace.execution.automaticEnabled = true;
      workspace.execution.paused = false;
      store.writeConfig(config);
      return workspace;
    }
    if (command === 'pause' || command === 'resume') {
      workspace.execution.paused = command === 'pause';
      if (!workspace.execution.paused && state.active) throw new Error('请先核对当前任务状态');
      store.writeConfig(config);
      return workspace;
    }

    if (command === 'prepare' || command === 'start') {
      const run = runContext(input);
      if (state.active && run.trigger !== 'preview') {
        if (
          run.trigger === 'scheduled' &&
          workspace.execution.automaticEnabled &&
          !workspace.execution.paused &&
          run.hostPlanRef === workspace.schedule?.hostPlanRef &&
          codexAdapter.inspectWorkspace(workspace, run) === 'match'
        ) {
          store.writeRun(id, { ...state, pendingCheck: true });
        }
        return { mode: 'blocked', reason: 'workspace-busy' };
      }
      const decision = prepareRun(
        workspace,
        codexAdapter,
        state.last?.outcome === 'unknown-delivery' && run.activity === 'idle'
          ? { ...run, activity: 'unknown' }
          : run,
        now,
      );
      if (state.last?.outcome === 'unknown-delivery' && decision.mode === 'read-only') {
        return { ...decision, reason: 'unknown-delivery', previous: state.last };
      }
      if (command === 'prepare' || decision.mode !== 'execute') return decision;
      const taskRef = string(input.taskRef, '任务引用');
      if (state.last?.outcome === 'completed' && state.last.taskRef === taskRef) {
        return { mode: 'blocked', reason: 'task-already-completed' };
      }
      const runId = randomUUID();
      store.writeRun(id, {
        ...state,
        pendingCheck: false,
        active: {
          id: runId,
          taskRef,
          startedAt: now.toISOString(),
          deliveryBinding: deliveryBinding(workspace),
        },
      });
      return { ...decision, runId };
    }

    if (command === 'reconcile') {
      if (
        state.active ||
        state.last?.outcome !== 'unknown-delivery' ||
        !state.last.deliveryBinding ||
        state.last.deliveryBinding !== deliveryBinding(workspace)
      ) {
        throw new Error('当前配置缺少可核对的原交付能力');
      }
      if (string(input.taskRef, '任务引用') !== state.last.taskRef)
        throw new Error('任务引用与待核对任务不符');
      const run = runContext(input);
      if (
        codexAdapter.inspectWorkspace(workspace, run) === 'mismatch' ||
        !checkOperation(workspace, run.availableRefs, 'delivery', 'observe')
      ) {
        throw new Error('当前会话无法使用原交付能力核对结果');
      }
      const absent = input.deliveryAbsent === true;
      const deliveryRef = absent ? undefined : string(input.deliveryRef, '已核对的交付引用');
      const detail = absent ? string(input.detail, '确认未交付的依据') : state.last.detail;
      if (absent && input.deliveryRef !== undefined) throw new Error('交付引用与未交付结果冲突');
      const last = {
        ...state.last,
        outcome: absent ? ('failed' as const) : ('completed' as const),
        detail,
        deliveryRef,
        recordedAt: now.toISOString(),
      };
      store.writeRun(id, { ...state, last });
      return last;
    }

    const active = state.active;
    if (!active || input.runId !== active.id) throw new Error('活动运行 ID 不匹配');
    if (command === 'check-action') {
      const action = string(input.action, '操作');
      const role = input.role as CapabilityRole;
      const run = runContext(input);
      const matched = codexAdapter.inspectWorkspace(workspace, run) === 'match';
      const allowed =
        input.role === undefined
          ? ['modify', 'verify'].includes(action) &&
            workspace.execution.allowedActions.includes(action) &&
            (action !== 'modify' || withPathScope(workspace, input.paths))
          : capabilityRoles.includes(role) &&
            checkOperation(workspace, run.availableRefs, role, action);
      return { allowed: matched && allowed };
    }
    if (command === 'finish' || command === 'recover') {
      const observed = summary(input);
      if (
        command === 'recover' &&
        (input.activity !== 'idle' ||
          !observed.detail ||
          !['cancelled', 'unknown-delivery'].includes(observed.outcome))
      ) {
        throw new Error('恢复需要宿主确认已停止、核对说明及取消或待核对结果');
      }
      const last = {
        ...observed,
        taskRef: active.taskRef,
        deliveryBinding: active.deliveryBinding,
        recordedAt: now.toISOString(),
      };
      store.writeRun(id, { pendingCheck: state.pendingCheck, last });
      return last;
    }
    throw new Error('未知命令');
  };
  if (
    command === 'status' ||
    command === 'plan' ||
    command === 'list' ||
    (command === 'prepare' && (input.trigger === undefined || input.trigger === 'preview'))
  ) {
    return execute();
  }
  return store.update(execute);
}
