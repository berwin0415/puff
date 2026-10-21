/** I23 crash trust checks. Only original naturally persisted bytes may enter native recovery. */
import { createHash } from 'node:crypto'
import { constants, closeSync, fstatSync, lstatSync, openSync, readFileSync, readdirSync } from 'node:fs'
import { resolve } from 'node:path'
import { checkedExistingPath, readTrustedJson, sameScope, validateDetailedOptions } from './detailed-options.mjs'

export const sha256 = bytes => createHash('sha256').update(bytes).digest('hex')
export function canonicalJson(value) {
  if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']'
  if (value !== null && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}'
  return JSON.stringify(value)
}
const hashObject = value => sha256(canonicalJson(value))
const id = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u
const digest = /^[a-f0-9]{64}$/u
function fields(value, required, optional = []) {
  if (!value || typeof value !== 'object' || Array.isArray(value) || required.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) throw new Error('Invalid Goal crash binding fields')
}
function checkedHash(value) {
  if (typeof value !== 'string' || !digest.test(value)) throw new Error('Invalid crash digest')
  return value
}
export function validateCrashControl(value) {
  fields(value, ['mode', 'chain_id', 'attempt_id', 'binding_file'])
  if (!['new', 'crash-resume'].includes(value.mode) || !id.test(value.chain_id) || value.chain_id.toLowerCase() === 'old' || value.attempt_id !== (value.mode === 'new' ? 'worker-a' : 'worker-b') || value.binding_file !== '/config/fault-goal.json') throw new Error('Invalid fault_recovery request')
  return Object.freeze(structuredClone(value))
}
/** Validation is separate from boot so malformed resume input cannot dispatch work. */
export function validateFaultGoalRequest(input) {
  const value = structuredClone(input)
  const keys = ['run_id', 'session_id', 'workspace', 'state_dir', 'output_dir', 'tools_url', 'tool_token_file', 'max_output_tokens', 'timeout_seconds', 'max_retries', 'candidate_options', 'fault_recovery']
  if (!value || typeof value !== 'object' || Array.isArray(value) || keys.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !keys.includes(key) && key !== 'prompt')) throw new Error('Invalid independent Goal run request')
  value.fault_recovery = validateCrashControl(value.fault_recovery)
  for (const key of ['run_id', 'session_id']) if (typeof value[key] !== 'string' || !id.test(value[key]) || value[key].toLowerCase() === 'old') throw new Error('Invalid Goal run/session identity')
  if (value.fault_recovery.mode === 'new') {
    if (typeof value.prompt !== 'string' || value.prompt.trim().length === 0 || value.prompt.length > 65536) throw new Error('A new Goal requires a bounded objective')
  } else {
    if (Object.hasOwn(value, 'prompt')) throw new Error('Resume forbids a prompt or continue message')
    value.prompt = '' // Internal compatibility; never submitted by the resume path.
  }
  if (!Number.isSafeInteger(value.max_output_tokens) || value.max_output_tokens < 1 || value.max_output_tokens > 16384 || !Number.isSafeInteger(value.timeout_seconds) || value.timeout_seconds < 1 || value.timeout_seconds > 300 || !Number.isSafeInteger(value.max_retries) || value.max_retries < 0 || value.max_retries > 2) throw new Error('Invalid Goal run budget')
  if (value.tools_url !== 'http://tools:8081' || value.tool_token_file !== '/config/scope.token') throw new Error('Unexpected trusted Goal tools route')
  for (const key of ['workspace', 'state_dir', 'output_dir', 'tool_token_file']) value[key] = checkedExistingPath(value[key])
  const paths = [value.workspace, value.state_dir, value.output_dir]
  if (new Set(paths).size !== paths.length || paths.some((path, i) => paths.some((other, j) => i !== j && path.startsWith(other + '/')))) throw new Error('Goal state/workspace/evidence must be disjoint')
  value.candidate_options = validateDetailedOptions(value.candidate_options)
  if (value.candidate_options.case !== 'tools' || value.candidate_options.session_action !== undefined || value.candidate_options.followup !== undefined) throw new Error('Goal crash supports the frozen tools case without legacy followup/resume options')
  return value
}
function fileBytes(filename, limit = 64 * 1024 * 1024) {
  const descriptor = openSync(checkedExistingPath(filename), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const info = fstatSync(descriptor)
    if (!info.isFile() || info.size > limit) throw new Error('Crash input is not a bounded regular file')
    return readFileSync(descriptor)
  } finally { closeSync(descriptor) }
}
function boundJson(filename, expectedHash) {
  const bytes = fileBytes(filename, 2 * 1024 * 1024)
  if (sha256(bytes) !== checkedHash(expectedHash)) throw new Error('Crash bound file changed')
  return JSON.parse(bytes.toString('utf8'))
}
/** Entire state/workspace mount inventory; do not omit WAL, journals or extra files. */
export function stateInventory(request) {
  const files = [], directories = []
  let total = 0
  const walk = (rootName, root, relative = '') => {
    const path = resolve(root, relative)
    const info = lstatSync(path)
    if (info.isSymbolicLink()) throw new Error('Crash state may not contain symbolic links')
    if (info.isDirectory()) {
      directories.push({ root: rootName, path: relative })
      for (const name of readdirSync(path).sort()) walk(rootName, root, relative ? relative + '/' + name : name)
    } else {
      if (!info.isFile() || info.nlink !== 1 || files.length >= 4096) throw new Error('Unsupported fault_recovery state file')
      const bytes = fileBytes(path)
      total += bytes.length
      if (total > 256 * 1024 * 1024) throw new Error('Crash state exceeds its frozen bound')
      files.push({ root: rootName, path: relative, bytes: bytes.length, sha256: sha256(bytes) })
    }
  }
  walk('state', checkedExistingPath(request.state_dir))
  walk('workspace', checkedExistingPath(request.workspace))
  return { schema: 'harness-lab-native-state-index/1', files, directories }
}
export function readFaultGoalBinding(request, provenance, scope) {
  const fault_recovery = validateCrashControl(request.fault_recovery)
  const binding = readTrustedJson(fault_recovery.binding_file)
  fields(binding, ['schema', 'candidate', 'variant', 'mode', 'chain_id', 'attempt_id', 'scope', 'session_id', 'materials', 'image', 'model', 'tools', 'state', 'goal', 'active_budget'], ['recovery'])
  if (binding.schema !== 'harness-lab-dsh-crash-binding/1' || binding.candidate !== 'deepseek_harness' || binding.variant !== 'fault-goal-runtime' || binding.mode !== fault_recovery.mode || binding.chain_id !== fault_recovery.chain_id || binding.chain_id !== request.run_id || binding.chain_id !== scope.variant || binding.attempt_id !== fault_recovery.attempt_id || binding.session_id !== request.session_id || !sameScope(binding.scope, scope)) throw new Error('Crash chain/attempt/full-scope mismatch')
  fields(binding.materials, ['source_export_sha256', 'adapter_export_sha256'])
  for (const key of ['source_export_sha256', 'adapter_export_sha256']) if (checkedHash(binding.materials[key]) !== provenance[key]) throw new Error('Crash source/adapter material mismatch')
  fields(binding.image, ['original_image_id'])
  if (!/^sha256:[a-f0-9]{64}$/u.test(binding.image.original_image_id) || process.env.HARNESS_I23_IMAGE_ID !== binding.image.original_image_id) throw new Error('Crash image declaration mismatch')
  fields(binding.model, ['config_sha256', 'run_token_sha256', 'budget_id', 'max_model_calls'])
  if (binding.model.budget_id !== binding.chain_id || binding.model.max_model_calls !== 20) throw new Error('Wrong shared model budget binding')
  checkedHash(binding.model.run_token_sha256)
  const model = { provider: 'deepseek-official', base_url: process.env.MODEL_API_BASE_URL, model: process.env.MODEL_NAME,
    max_output_tokens: request.max_output_tokens, max_retries: request.max_retries, thinking: 'disabled', reasoning_effort: 'off' }
  if (hashObject(model) !== checkedHash(binding.model.config_sha256)) throw new Error('Crash model configuration mismatch')
  fields(binding.tools, ['config_sha256', 'contract_sha256'])
  const tools = { url: request.tools_url, scope, token_sha256: sha256(fileBytes(request.tool_token_file, 8192).toString('utf8').trim()) }
  if (hashObject(tools) !== checkedHash(binding.tools.config_sha256)) throw new Error('Crash tool configuration/full-scope mismatch')
  checkedHash(binding.tools.contract_sha256)
  fields(binding.state, ['index_file', 'index_sha256'])
  if (binding.state.index_file !== '/config/fault-state.json') throw new Error('Unexpected fault_recovery state index path')
  const expectedState = boundJson(binding.state.index_file, binding.state.index_sha256)
  if (canonicalJson(expectedState) !== canonicalJson(stateInventory(request))) throw new Error('Crash complete state/workspace inventory mismatch')
  fields(binding.goal, ['max_goal_rounds'])
  if (!Number.isSafeInteger(binding.goal.max_goal_rounds) || binding.goal.max_goal_rounds < 1 || binding.goal.max_goal_rounds > 20) throw new Error('Invalid frozen Goal round cap')
  fields(binding.active_budget, ['limit_seconds', 'used_seconds', 'remaining_seconds'])
  const budget = binding.active_budget
  if (budget.limit_seconds !== 300 || !Number.isFinite(budget.used_seconds) || !Number.isFinite(budget.remaining_seconds) || budget.used_seconds < 0 || budget.remaining_seconds <= 0 || budget.used_seconds + budget.remaining_seconds > 300 || request.timeout_seconds > budget.remaining_seconds) throw new Error('No valid remaining cumulative active-time budget')
  let recovery
  if (fault_recovery.mode === 'crash-resume') {
    recovery = binding.recovery
    fields(recovery, ['boundary_sha256', 'audit_sha256', 'physical_path', 'physical_bytes', 'physical_sha256',
      'committed_bytes', 'committed_prefix_sha256', 'events_sha256', 'next_seq',
      'expected_repaired_events_sha256', 'expected_repaired_next_seq', 'durable_goal'])
    for (const key of ['boundary_sha256', 'audit_sha256', 'physical_sha256', 'committed_prefix_sha256', 'events_sha256', 'expected_repaired_events_sha256']) checkedHash(recovery[key])
    if (typeof recovery.physical_path !== 'string' || !/^sessions\/[^\\]+\.jsonl$/u.test(recovery.physical_path)
      || recovery.physical_path.split('/').some(part => !part || part === '.' || part === '..')
      || [recovery.physical_bytes, recovery.committed_bytes, recovery.next_seq, recovery.expected_repaired_next_seq].some(value => !Number.isSafeInteger(value) || value < 1)
      || recovery.committed_bytes > recovery.physical_bytes || recovery.physical_bytes > 64 * 1024 * 1024
      || recovery.expected_repaired_next_seq < recovery.next_seq) throw new Error('Invalid original crash prefix binding')
    const goal = recovery.durable_goal
    fields(goal, ['id', 'revision', 'objective', 'phase', 'maxGoalRounds', 'roundsStarted', 'createdAt', 'updatedAt'], ['blockedReason'])
    if (typeof goal.id !== 'string' || !goal.id || typeof goal.objective !== 'string' || !goal.objective
      || !['active', 'paused', 'blocked'].includes(goal.phase) || goal.maxGoalRounds !== binding.goal.max_goal_rounds
      || !Number.isSafeInteger(goal.revision) || goal.revision < 1 || !Number.isSafeInteger(goal.roundsStarted)
      || goal.roundsStarted < 1 || goal.roundsStarted >= goal.maxGoalRounds || Object.hasOwn(goal, 'activation')) throw new Error('Crash recovery requires the original unfinished durable Goal; activation is process-local')
    const bytes = fileBytes(resolve(request.state_dir, recovery.physical_path))
    if (sha256(bytes) !== recovery.physical_sha256 || bytes.length !== recovery.physical_bytes
      || sha256(bytes.subarray(0, recovery.committed_bytes)) !== recovery.committed_prefix_sha256) throw new Error('Original crash artifact changed before native boot')
    if (expectedState.files.some(file => file.root === 'state' && /(?:^|\/)(?:pause-index|lifecycle-pause[^/]*)\.json$/u.test(file.path))) throw new Error('Safe pause index cannot substitute for crash durability')
  } else if (binding.recovery !== undefined) throw new Error('A new Goal run must not inherit recovery data')
  return Object.freeze({ fault_recovery, binding, recovery, image_verification: 'trusted_declaration_match_only_host_must_verify_actual_docker_image_id',
    verifyToken(token) { if (sha256(token) !== binding.model.run_token_sha256) throw new Error('Shared model run token changed') },
    verifyToolContract(schemas) {
      if (hashObject([...schemas].sort((a, b) => a.name.localeCompare(b.name, 'en'))) !== binding.tools.contract_sha256) throw new Error('Actual native Goal/tool surface changed')
    } })
}
