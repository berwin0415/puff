/** Draft I22 trust checks. No candidate runtime, credentials or model dispatch. */
import { createHash } from 'node:crypto'
import { constants, closeSync, fstatSync, lstatSync, openSync, readFileSync, readdirSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
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
  if (!value || typeof value !== 'object' || Array.isArray(value) || required.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) throw new Error('Invalid Goal lifecycle binding fields')
}
function checkedHash(value) {
  if (typeof value !== 'string' || !digest.test(value)) throw new Error('Invalid lifecycle digest')
  return value
}
export function validateLifecycle(value) {
  fields(value, ['mode', 'chain_id', 'attempt_id', 'binding_file'])
  if (!['new', 'resume'].includes(value.mode) || !id.test(value.chain_id) || value.chain_id.toLowerCase() === 'old' || value.attempt_id !== (value.mode === 'new' ? 'worker-a' : 'worker-b') || value.binding_file !== '/config/lifecycle.json') throw new Error('Invalid lifecycle request')
  return Object.freeze(structuredClone(value))
}
/** Validation is separate from boot so malformed resume input cannot dispatch work. */
export function validateGoalRequest(input) {
  const value = structuredClone(input)
  const keys = ['run_id', 'session_id', 'workspace', 'state_dir', 'output_dir', 'tools_url', 'tool_token_file', 'max_output_tokens', 'timeout_seconds', 'max_retries', 'candidate_options', 'lifecycle']
  if (!value || typeof value !== 'object' || Array.isArray(value) || keys.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !keys.includes(key) && key !== 'prompt')) throw new Error('Invalid independent Goal run request')
  value.lifecycle = validateLifecycle(value.lifecycle)
  for (const key of ['run_id', 'session_id']) if (typeof value[key] !== 'string' || !id.test(value[key]) || value[key].toLowerCase() === 'old') throw new Error('Invalid Goal run/session identity')
  if (value.lifecycle.mode === 'new') {
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
  if (value.candidate_options.case !== 'tools' || value.candidate_options.session_action !== undefined || value.candidate_options.followup !== undefined) throw new Error('Goal lifecycle supports the frozen tools case without legacy followup/resume options')
  return value
}
function fileBytes(filename, limit = 64 * 1024 * 1024) {
  const descriptor = openSync(checkedExistingPath(filename), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const info = fstatSync(descriptor)
    if (!info.isFile() || info.size > limit) throw new Error('Lifecycle input is not a bounded regular file')
    return readFileSync(descriptor)
  } finally { closeSync(descriptor) }
}
function boundJson(filename, expectedHash) {
  const bytes = fileBytes(filename, 2 * 1024 * 1024)
  if (sha256(bytes) !== checkedHash(expectedHash)) throw new Error('Lifecycle bound file changed')
  return JSON.parse(bytes.toString('utf8'))
}
/** Entire state/workspace mount inventory; do not omit WAL, journals or extra files. */
export function stateInventory(request) {
  const files = [], directories = []
  let total = 0
  const walk = (rootName, root, relative = '') => {
    const path = resolve(root, relative)
    const info = lstatSync(path)
    if (info.isSymbolicLink()) throw new Error('Lifecycle state may not contain symbolic links')
    if (info.isDirectory()) {
      directories.push({ root: rootName, path: relative })
      for (const name of readdirSync(path).sort()) walk(rootName, root, relative ? relative + '/' + name : name)
    } else {
      if (!info.isFile() || info.nlink !== 1 || files.length >= 4096) throw new Error('Unsupported lifecycle state file')
      const bytes = fileBytes(path)
      total += bytes.length
      if (total > 256 * 1024 * 1024) throw new Error('Lifecycle state exceeds its frozen bound')
      files.push({ root: rootName, path: relative, bytes: bytes.length, sha256: sha256(bytes) })
    }
  }
  walk('state', checkedExistingPath(request.state_dir))
  walk('workspace', checkedExistingPath(request.workspace))
  return { schema: 'harness-lab-native-state-index/1', files, directories }
}
export function readGoalBinding(request, provenance, scope) {
  const lifecycle = validateLifecycle(request.lifecycle)
  const binding = readTrustedJson(lifecycle.binding_file)
  fields(binding, ['schema', 'candidate', 'variant', 'mode', 'chain_id', 'attempt_id', 'scope', 'session_id', 'materials', 'image', 'model', 'tools', 'state', 'goal', 'active_budget'], ['resume'])
  if (binding.schema !== 'harness-lab-lifecycle/1' || binding.candidate !== 'deepseek_harness' || binding.variant !== 'goal-runtime' || binding.mode !== lifecycle.mode || binding.chain_id !== lifecycle.chain_id || binding.chain_id !== request.run_id || binding.chain_id !== scope.variant || binding.attempt_id !== lifecycle.attempt_id || binding.session_id !== request.session_id || !sameScope(binding.scope, scope)) throw new Error('Lifecycle chain/attempt/full-scope mismatch')
  fields(binding.materials, ['source_export_sha256', 'adapter_export_sha256'])
  for (const key of ['source_export_sha256', 'adapter_export_sha256']) if (checkedHash(binding.materials[key]) !== provenance[key]) throw new Error('Lifecycle source/adapter material mismatch')
  fields(binding.image, ['original_image_id'])
  if (!/^sha256:[a-f0-9]{64}$/u.test(binding.image.original_image_id) || process.env.HARNESS_LIFECYCLE_IMAGE_ID !== binding.image.original_image_id) throw new Error('Lifecycle image declaration mismatch')
  fields(binding.model, ['config_sha256', 'run_token_sha256', 'budget_id', 'max_model_calls'])
  if (binding.model.budget_id !== binding.chain_id || binding.model.max_model_calls !== 20) throw new Error('Wrong shared model budget binding')
  checkedHash(binding.model.run_token_sha256)
  const model = { provider: 'deepseek-official', base_url: process.env.MODEL_API_BASE_URL, model: process.env.MODEL_NAME,
    max_output_tokens: request.max_output_tokens, max_retries: request.max_retries, thinking: 'disabled', reasoning_effort: 'off' }
  if (hashObject(model) !== checkedHash(binding.model.config_sha256)) throw new Error('Lifecycle model configuration mismatch')
  fields(binding.tools, ['config_sha256', 'contract_sha256'])
  const tools = { url: request.tools_url, scope, token_sha256: sha256(fileBytes(request.tool_token_file, 8192).toString('utf8').trim()) }
  if (hashObject(tools) !== checkedHash(binding.tools.config_sha256)) throw new Error('Lifecycle tool configuration/full-scope mismatch')
  checkedHash(binding.tools.contract_sha256)
  fields(binding.state, ['index_file', 'index_sha256'])
  if (binding.state.index_file !== '/config/lifecycle-state.json') throw new Error('Unexpected lifecycle state index path')
  const expectedState = boundJson(binding.state.index_file, binding.state.index_sha256)
  if (canonicalJson(expectedState) !== canonicalJson(stateInventory(request))) throw new Error('Lifecycle complete state/workspace inventory mismatch')
  fields(binding.goal, ['max_goal_rounds'])
  if (!Number.isSafeInteger(binding.goal.max_goal_rounds) || binding.goal.max_goal_rounds < 1 || binding.goal.max_goal_rounds > 20) throw new Error('Invalid frozen Goal round cap')
  fields(binding.active_budget, ['limit_seconds', 'used_seconds', 'remaining_seconds'])
  const budget = binding.active_budget
  if (budget.limit_seconds !== 300 || !Number.isFinite(budget.used_seconds) || !Number.isFinite(budget.remaining_seconds) || budget.used_seconds < 0 || budget.remaining_seconds <= 0 || budget.used_seconds + budget.remaining_seconds > 300 || request.timeout_seconds > budget.remaining_seconds) throw new Error('No valid remaining cumulative active-time budget')
  let paused
  if (lifecycle.mode === 'resume') {
    fields(binding.resume, ['pause_index_file', 'pause_index_sha256'])
    if (binding.resume.pause_index_file !== '/config/lifecycle-pause.json') throw new Error('Unexpected trusted pause index path')
    paused = boundJson(binding.resume.pause_index_file, binding.resume.pause_index_sha256)
    fields(paused, ['schema', 'candidate', 'chain_id', 'scope', 'session_id', 'goal', 'session', 'source'])
    if (paused.schema !== 'harness-lab-goal-pause/1' || paused.candidate !== 'deepseek_harness' || paused.chain_id !== binding.chain_id || paused.session_id !== request.session_id || !sameScope(paused.scope, scope) || paused.goal?.phase !== 'paused' || paused.goal?.activation !== 'disarmed' || paused.goal?.maxGoalRounds !== binding.goal.max_goal_rounds) throw new Error('Trusted pause index belongs to another native Goal or scope')
    if (typeof paused.goal.objective !== 'string' || paused.goal.objective.length < 1 || !Number.isSafeInteger(paused.goal.revision) || !Number.isSafeInteger(paused.goal.roundsStarted) || paused.goal.roundsStarted < 1 || paused.goal.roundsStarted >= paused.goal.maxGoalRounds) throw new Error('Goal pause index is not resumable within its frozen rounds')
    fields(paused.session, ['id', 'cwd', 'next_seq', 'artifact_sha256', 'persisted_events_sha256'])
    if (paused.session.id !== request.session_id || paused.session.cwd !== request.workspace || !Number.isSafeInteger(paused.session.next_seq) || paused.session.next_seq < 1) throw new Error('Invalid paused native Session index')
    checkedHash(paused.session.artifact_sha256)
    checkedHash(paused.session.persisted_events_sha256)
  } else if (binding.resume !== undefined) throw new Error('A new Goal run must not inherit resume data')
  return Object.freeze({ lifecycle, binding, paused, image_verification: 'trusted_declaration_match_only_host_must_verify_actual_docker_image_id',
    verifyToken(token) { if (sha256(token) !== binding.model.run_token_sha256) throw new Error('Shared model run token changed') },
    verifyToolContract(schemas) {
      if (hashObject([...schemas].sort((a, b) => a.name.localeCompare(b.name, 'en'))) !== binding.tools.contract_sha256) throw new Error('Actual native Goal/tool surface changed')
    } })
}
