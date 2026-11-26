import type { CapabilityRole, WorkspaceBinding } from './config.js';
import type { ExecutionContext, HostAdapter, HostRunContext } from './host-contract.js';

export function insideWindow(workspace: WorkspaceBinding, now: Date): boolean {
  const parts = new Intl.DateTimeFormat('en-US', {
    timeZone: workspace.execution.timezone,
    weekday: 'short',
    hour: '2-digit',
    minute: '2-digit',
    hourCycle: 'h23',
  }).formatToParts(now);
  const day =
    ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'].indexOf(
      parts.find((part) => part.type === 'weekday')?.value ?? '',
    ) + 1;
  const minute =
    Number(parts.find((part) => part.type === 'hour')?.value) * 60 +
    Number(parts.find((part) => part.type === 'minute')?.value);
  const previousDay = day === 1 ? 7 : day - 1;
  return workspace.execution.windows.some(({ days, startMinute, endMinute }) => {
    if (startMinute < endMinute)
      return days.includes(day) && minute >= startMinute && minute < endMinute;
    return (
      (days.includes(day) && minute >= startMinute) ||
      (days.includes(previousDay) && minute < endMinute)
    );
  });
}

export function checkOperation(
  workspace: WorkspaceBinding,
  availableRefs: readonly string[],
  role: CapabilityRole,
  operation: string,
) {
  return (
    workspace.execution.allowedActions.includes(operation) &&
    workspace.capabilities.some(
      (binding) =>
        binding.enabled &&
        binding.role === role &&
        availableRefs.includes(binding.ref) &&
        binding.operations.includes(operation),
    )
  );
}

export type RunDecision =
  | { mode: 'blocked'; reason: string }
  | { mode: 'read-only' | 'execute'; reason?: string; context: ExecutionContext };

export function prepareRun(
  workspace: WorkspaceBinding,
  host: HostAdapter,
  run: HostRunContext,
  now = new Date(),
): RunDecision {
  if (workspace.host.type !== host.type || workspace.host.adapterVersion !== host.version) {
    return { mode: 'blocked', reason: 'host-mismatch' };
  }
  const location = host.inspectWorkspace(workspace, run);
  if (location === 'mismatch') return { mode: 'blocked', reason: 'workspace-mismatch' };
  if (!checkOperation(workspace, run.availableRefs, 'task-source', 'read')) {
    return { mode: 'blocked', reason: 'missing-task-source' };
  }

  const context: ExecutionContext = {
    workspaceId: workspace.id,
    cwd: run.cwd,
    taskScope: workspace.execution.taskScope,
    allowedPaths: workspace.execution.allowedPaths,
    allowedActions: workspace.execution.allowedActions,
    capabilities: workspace.capabilities
      .filter((binding) => binding.enabled && run.availableRefs.includes(binding.ref))
      .map((binding) => ({
        ...binding,
        operations: binding.operations.filter((operation) =>
          workspace.execution.allowedActions.includes(operation),
        ),
      })),
  };
  const readOnly = (reason: string): RunDecision => ({
    mode: 'read-only',
    reason,
    context: {
      ...context,
      allowedPaths: [],
      allowedActions: context.allowedActions.filter((action) =>
        ['list', 'read', 'observe'].includes(action),
      ),
      capabilities: context.capabilities.map((binding) => ({
        ...binding,
        operations: binding.operations.filter((operation) =>
          ['list', 'read', 'observe'].includes(operation),
        ),
      })),
    },
  });
  if (run.trigger === 'preview') return readOnly('preview');
  if (workspace.execution.paused) return { mode: 'blocked', reason: 'paused' };
  if (
    run.trigger === 'scheduled' &&
    (!workspace.execution.automaticEnabled ||
      !run.hostPlanRef ||
      run.hostPlanRef !== workspace.schedule?.hostPlanRef)
  ) {
    return { mode: 'blocked', reason: 'plan-disabled-or-mismatched' };
  }
  if (!insideWindow(workspace, now)) return { mode: 'blocked', reason: 'outside-window' };
  if (run.activity === 'busy') return { mode: 'blocked', reason: 'workspace-busy' };
  if (run.activity === 'unknown') return readOnly('host-activity-unknown');
  if (location === 'preview-only') return readOnly('worktree-required');
  if (
    !workspace.execution.allowedActions.includes('modify') ||
    !workspace.execution.allowedPaths.length
  ) {
    return readOnly('workspace-read-only');
  }
  return { mode: 'execute', context };
}
