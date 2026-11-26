export const capabilityRoles = ['task-source', 'delivery', 'notification', 'review'] as const;
export const capabilityKinds = ['skill', 'mcp', 'cli', 'host'] as const;
export type CapabilityRole = (typeof capabilityRoles)[number];
export type CapabilityKind = (typeof capabilityKinds)[number];

export interface CapabilityBinding {
  role: CapabilityRole;
  ref: string;
  kind: CapabilityKind;
  operations: string[];
  enabled: boolean;
}

export interface TimeWindow {
  days: number[];
  startMinute: number;
  endMinute: number;
}

export interface WorkspaceBinding {
  id: string;
  rootPath: string;
  host: { type: string; adapterVersion: string };
  capabilities: CapabilityBinding[];
  execution: {
    automaticEnabled: boolean;
    maxActiveRuns: 1;
    workspaceMode: 'local' | 'worktree';
    taskScope: string;
    allowedPaths: string[];
    allowedActions: string[];
    timezone: string;
    windows: TimeWindow[];
    paused?: boolean;
  };
  schedule?: { intervalMinutes: number; hostPlanRef?: string };
}

export interface PluginConfig {
  schemaVersion: 1;
  workspaces: WorkspaceBinding[];
}

const operationsByRole: Record<CapabilityRole, readonly string[]> = {
  'task-source': ['list', 'read', 'claim', 'writeback'],
  delivery: ['prepare', 'publish', 'observe'],
  notification: ['report'],
  review: ['review', 'observe'],
};

export const workspaceIdPattern = /^[a-z0-9][a-z0-9-]{0,63}$/;

function object(value: unknown, keys: string[], label: string): Record<string, unknown> {
  if (!value || typeof value !== 'object' || Array.isArray(value))
    throw new Error(`${label} 必须是对象`);
  const fields = value as Record<string, unknown>;
  if (Object.keys(fields).some((key) => !keys.includes(key)))
    throw new Error(`${label} 包含不支持的字段`);
  return fields;
}

function string(value: unknown, label: string): string {
  if (
    typeof value !== 'string' ||
    !value.trim() ||
    [...value].some((char) => char.charCodeAt(0) < 32)
  ) {
    throw new Error(`${label} 必须是非空字符串`);
  }
  return value;
}

function array(value: unknown, label: string): unknown[] {
  if (!Array.isArray(value)) throw new Error(`${label} 必须是数组`);
  return value;
}

function integer(value: unknown, min: number, max: number, label: string): number {
  if (typeof value !== 'number' || !Number.isInteger(value) || value < min || value > max) {
    throw new Error(`${label} 超出允许范围`);
  }
  return value;
}

function boolean(value: unknown, label: string): boolean {
  if (typeof value !== 'boolean') throw new Error(`${label} 必须为布尔值`);
  return value;
}

function choice<T extends string>(value: unknown, options: readonly T[], label: string): T {
  const matched = options.find((option) => option === value);
  if (matched === undefined) throw new Error(`${label} 不受支持`);
  return matched;
}

function capability(value: unknown): CapabilityBinding {
  const fields = object(value, ['role', 'ref', 'kind', 'operations', 'enabled'], '能力绑定');
  const role = choice(fields.role, capabilityRoles, '能力角色');
  const ref = string(fields.ref, '能力引用');
  if (!/^[\w@.:/-]+$/.test(ref) || ref.includes('://')) throw new Error('能力引用必须是宿主标识');
  const operations = array(fields.operations, '能力操作').map((operation) =>
    choice(operation, operationsByRole[role], '能力操作'),
  );
  return {
    role,
    ref,
    kind: choice(fields.kind, capabilityKinds, '能力类型'),
    operations,
    enabled: boolean(fields.enabled, '能力启用状态'),
  };
}

function timeWindow(value: unknown): TimeWindow {
  const fields = object(value, ['days', 'startMinute', 'endMinute'], '运行窗口');
  const days = array(fields.days, '工作日').map((day) => integer(day, 1, 7, '工作日'));
  const startMinute = integer(fields.startMinute, 0, 1439, '窗口起点');
  const endMinute = integer(fields.endMinute, 0, 1440, '窗口终点');
  if (!days.length || startMinute === endMinute) throw new Error('运行窗口为空');
  return { days, startMinute, endMinute };
}

function allowedPath(value: unknown): string {
  const path = string(value, '允许修改路径');
  if (
    (path !== '.' &&
      path.split('/').some((segment) => !segment || segment === '.' || segment === '..')) ||
    path.includes('\\') ||
    /^[A-Za-z]:/.test(path)
  ) {
    throw new Error('允许修改路径必须相对于工作区');
  }
  return path;
}

function execution(value: unknown): WorkspaceBinding['execution'] {
  const fields = object(
    value,
    [
      'automaticEnabled',
      'maxActiveRuns',
      'workspaceMode',
      'taskScope',
      'allowedPaths',
      'allowedActions',
      'timezone',
      'windows',
      'paused',
    ],
    '执行规则',
  );
  if (fields.maxActiveRuns !== 1) throw new Error('v0.1 每个工作区只有一个活动名额');
  const timezone = string(fields.timezone, '时区');
  new Intl.DateTimeFormat('en', { timeZone: timezone });
  return {
    automaticEnabled: boolean(fields.automaticEnabled ?? false, '自动执行'),
    maxActiveRuns: 1,
    workspaceMode: choice(fields.workspaceMode, ['local', 'worktree'] as const, '工作区模式'),
    taskScope: string(fields.taskScope, '任务范围'),
    allowedPaths: array(fields.allowedPaths, '允许修改路径').map(allowedPath),
    allowedActions: array(fields.allowedActions, '允许操作').map((action) =>
      choice(
        action,
        [
          'list',
          'read',
          'claim',
          'modify',
          'verify',
          'prepare',
          'publish',
          'observe',
          'writeback',
          'report',
          'review',
        ] as const,
        '工作区操作',
      ),
    ),
    timezone,
    windows: array(fields.windows, '运行窗口').map(timeWindow),
    paused: boolean(fields.paused ?? false, '暂停状态'),
  };
}

function workspace(value: unknown): WorkspaceBinding {
  const fields = object(
    value,
    ['id', 'rootPath', 'host', 'capabilities', 'execution', 'schedule'],
    '工作区',
  );
  const id = string(fields.id, '工作区 ID');
  if (!workspaceIdPattern.test(id)) throw new Error('工作区 ID 只能使用小写字母、数字和连字符');
  const rootPath = string(fields.rootPath, '工作区路径');
  if (!rootPath.startsWith('/') && !/^(?:[A-Za-z]:[/\\]|\\\\)/.test(rootPath)) {
    throw new Error('工作区路径必须是绝对路径');
  }
  const host = object(fields.host, ['type', 'adapterVersion'], '宿主');
  const capabilities = array(fields.capabilities, '能力绑定').map(capability);
  const enabledRoles = capabilities
    .filter((binding) => binding.enabled)
    .map((binding) => binding.role);
  if (new Set(enabledRoles).size !== enabledRoles.length)
    throw new Error('每个角色只能启用一个绑定');
  const rule = execution(fields.execution);
  const result: WorkspaceBinding = {
    id,
    rootPath,
    host: {
      type: string(host.type, '宿主类型'),
      adapterVersion: string(host.adapterVersion, '适配版本'),
    },
    capabilities,
    execution: rule,
  };
  if (fields.schedule !== undefined) {
    const schedule = object(fields.schedule, ['intervalMinutes', 'hostPlanRef'], '宿主计划');
    result.schedule = { intervalMinutes: integer(schedule.intervalMinutes, 1, 1440, '检查间隔') };
    if (schedule.hostPlanRef !== undefined)
      result.schedule.hostPlanRef = string(schedule.hostPlanRef, '宿主计划引用');
  }
  if (rule.automaticEnabled && (!result.schedule?.hostPlanRef || !rule.windows.length)) {
    throw new Error('启用自动执行需要已绑定的计划与运行窗口');
  }
  return result;
}

export function parseConfig(value: unknown): PluginConfig {
  const fields = object(value, ['schemaVersion', 'workspaces'], '插件配置');
  if (fields.schemaVersion !== 1) throw new Error('配置版本不受支持');
  const workspaces = array(fields.workspaces, '工作区').map(workspace);
  if (new Set(workspaces.map(({ id }) => id)).size !== workspaces.length)
    throw new Error('工作区 ID 重复');
  const plans = workspaces.flatMap(({ schedule }) =>
    schedule?.hostPlanRef ? [schedule.hostPlanRef] : [],
  );
  if (new Set(plans).size !== plans.length) throw new Error('宿主计划只能绑定一个工作区');
  return { schemaVersion: 1, workspaces };
}
