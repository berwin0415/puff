import type { CapabilityBinding, WorkspaceBinding } from './config.js';

export type Trigger = 'manual' | 'scheduled' | 'preview';

export interface HostRunContext {
  trigger: Trigger;
  hostPlanRef?: string;
  activity: 'idle' | 'busy' | 'unknown';
  availableRefs: readonly string[];
  cwd: string;
}

export interface HostAdapter {
  type: string;
  version: string;
  inspectWorkspace(
    workspace: WorkspaceBinding,
    context: HostRunContext,
  ): 'match' | 'preview-only' | 'mismatch';
  planPrompt(workspace: WorkspaceBinding): string;
}

export interface RunSummary {
  outcome: 'completed' | 'blocked' | 'failed' | 'cancelled' | 'unknown-delivery';
  taskRef?: string;
  verification?: string;
  deliveryRef?: string;
  detail?: string;
}

export interface ExecutionContext {
  workspaceId: string;
  cwd: string;
  taskScope: string;
  allowedPaths: string[];
  allowedActions: string[];
  capabilities: CapabilityBinding[];
}
