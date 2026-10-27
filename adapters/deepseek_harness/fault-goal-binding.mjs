/** I23 故障可信检查；原生恢复只接受原始自然持久化字节。 */
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
  if (!value || typeof value !== 'object' || Array.isArray(value) || required.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !required.includes(key) && !optional.includes(key))) throw new Error('Goal 故障绑定字段无效')
}
function checkedHash(value) {
  if (typeof value !== 'string' || !digest.test(value)) throw new Error('故障摘要无效')
  return value
}
export function validateCrashControl(value) {
  fields(value, ['mode', 'chain_id', 'attempt_id', 'binding_file'])
  if (!['new', 'crash-resume'].includes(value.mode) || !id.test(value.chain_id) || value.chain_id.toLowerCase() === 'old' || value.attempt_id !== (value.mode === 'new' ? 'worker-a' : 'worker-b') || value.binding_file !== '/config/fault-goal.json') throw new Error('fault_recovery 请求无效')
  return Object.freeze(structuredClone(value))
}
/** 校验与启动分离，避免格式错误的恢复输入触发工作分发。 */
export function validateFaultGoalRequest(input) {
  const value = structuredClone(input)
  const keys = ['run_id', 'session_id', 'workspace', 'state_dir', 'output_dir', 'tools_url', 'tool_token_file', 'max_output_tokens', 'timeout_seconds', 'max_retries', 'candidate_options', 'fault_recovery']
  if (!value || typeof value !== 'object' || Array.isArray(value) || keys.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !keys.includes(key) && key !== 'prompt')) throw new Error('独立 Goal 运行请求无效')
  value.fault_recovery = validateCrashControl(value.fault_recovery)
  for (const key of ['run_id', 'session_id']) if (typeof value[key] !== 'string' || !id.test(value[key]) || value[key].toLowerCase() === 'old') throw new Error('Goal 运行或会话身份无效')
  if (value.fault_recovery.mode === 'new') {
    if (typeof value.prompt !== 'string' || value.prompt.trim().length === 0 || value.prompt.length > 65536) throw new Error('新 Goal 必须提供长度受限的目标')
  } else {
    if (Object.hasOwn(value, 'prompt')) throw new Error('恢复请求禁止包含提示词或继续消息')
    value.prompt = '' // 仅供内部兼容；恢复路径不会提交此值。
  }
  if (!Number.isSafeInteger(value.max_output_tokens) || value.max_output_tokens < 1 || value.max_output_tokens > 16384 || !Number.isSafeInteger(value.timeout_seconds) || value.timeout_seconds < 1 || value.timeout_seconds > 300 || !Number.isSafeInteger(value.max_retries) || value.max_retries < 0 || value.max_retries > 2) throw new Error('Goal 运行预算无效')
  if (value.tools_url !== 'http://tools:8081' || value.tool_token_file !== '/config/scope.token') throw new Error('可信 Goal 工具路由不符合预期')
  for (const key of ['workspace', 'state_dir', 'output_dir', 'tool_token_file']) value[key] = checkedExistingPath(value[key])
  const paths = [value.workspace, value.state_dir, value.output_dir]
  if (new Set(paths).size !== paths.length || paths.some((path, i) => paths.some((other, j) => i !== j && path.startsWith(other + '/')))) throw new Error('Goal 状态、工作区和证据目录必须相互独立')
  value.candidate_options = validateDetailedOptions(value.candidate_options)
  if (value.candidate_options.case !== 'tools' || value.candidate_options.session_action !== undefined || value.candidate_options.followup !== undefined) throw new Error('Goal 故障验证仅支持冻结的工具用例，不接受旧版 followup/resume 选项')
  return value
}
function fileBytes(filename, limit = 64 * 1024 * 1024) {
  const descriptor = openSync(checkedExistingPath(filename), constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
  try {
    const info = fstatSync(descriptor)
    if (!info.isFile() || info.size > limit) throw new Error('故障输入不是大小受限的普通文件')
    return readFileSync(descriptor)
  } finally { closeSync(descriptor) }
}
function boundJson(filename, expectedHash) {
  const bytes = fileBytes(filename, 2 * 1024 * 1024)
  if (sha256(bytes) !== checkedHash(expectedHash)) throw new Error('故障绑定文件发生变化')
  return JSON.parse(bytes.toString('utf8'))
}
/** 记录完整状态和工作区挂载清单，不遗漏 WAL、日志或额外文件。 */
export function stateInventory(request) {
  const files = [], directories = []
  let total = 0
  const walk = (rootName, root, relative = '') => {
    const path = resolve(root, relative)
    const info = lstatSync(path)
    if (info.isSymbolicLink()) throw new Error('故障状态不得包含符号链接')
    if (info.isDirectory()) {
      directories.push({ root: rootName, path: relative })
      for (const name of readdirSync(path).sort()) walk(rootName, root, relative ? relative + '/' + name : name)
    } else {
      if (!info.isFile() || info.nlink !== 1 || files.length >= 4096) throw new Error('不支持的 fault_recovery 状态文件')
      const bytes = fileBytes(path)
      total += bytes.length
      if (total > 256 * 1024 * 1024) throw new Error('故障状态超出冻结上限')
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
  if (binding.schema !== 'harness-lab-dsh-crash-binding/1' || binding.candidate !== 'deepseek_harness' || binding.variant !== 'fault-goal-runtime' || binding.mode !== fault_recovery.mode || binding.chain_id !== fault_recovery.chain_id || binding.chain_id !== request.run_id || binding.chain_id !== scope.variant || binding.attempt_id !== fault_recovery.attempt_id || binding.session_id !== request.session_id || !sameScope(binding.scope, scope)) throw new Error('故障执行链、尝试或完整作用域不匹配')
  fields(binding.materials, ['source_export_sha256', 'adapter_export_sha256'])
  for (const key of ['source_export_sha256', 'adapter_export_sha256']) if (checkedHash(binding.materials[key]) !== provenance[key]) throw new Error('故障源码或适配器材料不匹配')
  fields(binding.image, ['original_image_id'])
  if (!/^sha256:[a-f0-9]{64}$/u.test(binding.image.original_image_id) || process.env.HARNESS_I23_IMAGE_ID !== binding.image.original_image_id) throw new Error('故障镜像声明不匹配')
  fields(binding.model, ['config_sha256', 'run_token_sha256', 'budget_id', 'max_model_calls'])
  if (binding.model.budget_id !== binding.chain_id || binding.model.max_model_calls !== 20) throw new Error('共享模型预算绑定错误')
  checkedHash(binding.model.run_token_sha256)
  const model = { provider: 'deepseek-official', base_url: process.env.MODEL_API_BASE_URL, model: process.env.MODEL_NAME,
    max_output_tokens: request.max_output_tokens, max_retries: request.max_retries, thinking: 'disabled', reasoning_effort: 'off' }
  if (hashObject(model) !== checkedHash(binding.model.config_sha256)) throw new Error('故障模型配置不匹配')
  fields(binding.tools, ['config_sha256', 'contract_sha256'])
  const tools = { url: request.tools_url, scope, token_sha256: sha256(fileBytes(request.tool_token_file, 8192).toString('utf8').trim()) }
  if (hashObject(tools) !== checkedHash(binding.tools.config_sha256)) throw new Error('故障工具配置或完整作用域不匹配')
  checkedHash(binding.tools.contract_sha256)
  fields(binding.state, ['index_file', 'index_sha256'])
  if (binding.state.index_file !== '/config/fault-state.json') throw new Error('fault_recovery 状态索引路径不符合预期')
  const expectedState = boundJson(binding.state.index_file, binding.state.index_sha256)
  if (canonicalJson(expectedState) !== canonicalJson(stateInventory(request))) throw new Error('故障恢复的完整状态或工作区清单不匹配')
  fields(binding.goal, ['max_goal_rounds'])
  if (!Number.isSafeInteger(binding.goal.max_goal_rounds) || binding.goal.max_goal_rounds < 1 || binding.goal.max_goal_rounds > 20) throw new Error('冻结的 Goal 轮数上限无效')
  fields(binding.active_budget, ['limit_seconds', 'used_seconds', 'remaining_seconds'])
  const budget = binding.active_budget
  if (budget.limit_seconds !== 300 || !Number.isFinite(budget.used_seconds) || !Number.isFinite(budget.remaining_seconds) || budget.used_seconds < 0 || budget.remaining_seconds <= 0 || budget.used_seconds + budget.remaining_seconds > 300 || request.timeout_seconds > budget.remaining_seconds) throw new Error('没有有效的剩余累计活跃时间预算')
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
      || recovery.expected_repaired_next_seq < recovery.next_seq) throw new Error('原始故障前缀绑定无效')
    const goal = recovery.durable_goal
    fields(goal, ['id', 'revision', 'objective', 'phase', 'maxGoalRounds', 'roundsStarted', 'createdAt', 'updatedAt'], ['blockedReason'])
    if (typeof goal.id !== 'string' || !goal.id || typeof goal.objective !== 'string' || !goal.objective
      || !['active', 'paused', 'blocked'].includes(goal.phase) || goal.maxGoalRounds !== binding.goal.max_goal_rounds
      || !Number.isSafeInteger(goal.revision) || goal.revision < 1 || !Number.isSafeInteger(goal.roundsStarted)
      || goal.roundsStarted < 1 || goal.roundsStarted >= goal.maxGoalRounds || Object.hasOwn(goal, 'activation')) throw new Error('故障恢复必须使用原始未完成的持久化 Goal；activation 仅属于进程内状态')
    const bytes = fileBytes(resolve(request.state_dir, recovery.physical_path))
    if (sha256(bytes) !== recovery.physical_sha256 || bytes.length !== recovery.physical_bytes
      || sha256(bytes.subarray(0, recovery.committed_bytes)) !== recovery.committed_prefix_sha256) throw new Error('原生启动前，原始故障产物发生变化')
    if (expectedState.files.some(file => file.root === 'state' && /(?:^|\/)(?:pause-index|lifecycle-pause[^/]*)\.json$/u.test(file.path))) throw new Error('安全暂停索引不能替代故障持久化证据')
  } else if (binding.recovery !== undefined) throw new Error('新 Goal 运行不得继承故障恢复数据')
  return Object.freeze({ fault_recovery, binding, recovery, image_verification: 'trusted_declaration_match_only_host_must_verify_actual_docker_image_id',
    verifyToken(token) { if (sha256(token) !== binding.model.run_token_sha256) throw new Error('共享模型运行令牌发生变化') },
    verifyToolContract(schemas) {
      if (hashObject([...schemas].sort((a, b) => a.name.localeCompare(b.name, 'en'))) !== binding.tools.contract_sha256) throw new Error('实际原生 Goal 或工具接口发生变化')
    } })
}
