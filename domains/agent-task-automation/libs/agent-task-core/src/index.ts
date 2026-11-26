export {
  type CapabilityBinding,
  type CapabilityKind,
  type CapabilityRole,
  capabilityRoles,
  type PluginConfig,
  parseConfig,
  type TimeWindow,
  type WorkspaceBinding,
  workspaceIdPattern,
} from './config.js';
export { checkOperation, insideWindow, prepareRun, type RunDecision } from './execution-context.js';
export type {
  ExecutionContext,
  HostAdapter,
  HostRunContext,
  RunSummary,
  Trigger,
} from './host-contract.js';
