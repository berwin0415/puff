/** 仅校准适配器边界：使用内置 node:test 和模拟原生 API，不启动模型或上游运行时。 */
import assert from 'node:assert/strict'
import { appendFileSync, existsSync, linkSync, mkdirSync, mkdtempSync, readFileSync, realpathSync, rmSync, symlinkSync, unlinkSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { mock, test } from 'node:test'

const adapter = process.env.DSH_GOAL_ADAPTER_SOURCE ? pathToFileURL(resolve(process.env.DSH_GOAL_ADAPTER_SOURCE) + '/') : new URL('../../adapters/deepseek_harness/', import.meta.url)
const detailedURL = new URL('detailed-options.mjs', adapter)
const detailed = await import(detailedURL.href)
let configurationRoot
const mapped = filename => String(filename).startsWith('/config/') ? resolve(configurationRoot, String(filename).slice('/config/'.length)) : filename
// 仅转换容器挂载路径别名。文件系统检查、
// 禁止跟随链接的文件打开、字节、哈希和完整目录树清单均使用真实实现。
mock.module(detailedURL.href, { namedExports: { ...detailed,
  checkedExistingPath: filename => detailed.checkedExistingPath(mapped(filename)),
  readTrustedJson: filename => detailed.readTrustedJson(mapped(filename)),
} })
const { canonicalJson, sha256, readGoalBinding, stateInventory, validateGoalRequest } = await import(new URL('goal-binding.mjs', adapter).href)
const { GoalRuntime } = await import(new URL('goal-runtime.ts', adapter).href)
const objectHash = value => sha256(canonicalJson(value))
const schemas = [
  { name: 'write', description: '模拟写入工具结构', parameters: { type: 'object', properties: { value: { type: 'string' } } } },
  { name: 'get_goal', description: '模拟 Goal 工具结构', parameters: {} },
]
const sortedSchemas = () => structuredClone(schemas).sort((a, b) => a.name.localeCompare(b.name, 'en'))

function fixture(t, mode = 'new') {
  const root = mkdtempSync(resolve(realpathSync(tmpdir()), 'dsh-goal-boundary-'))
  configurationRoot = resolve(root, 'config')
  for (const name of ['config', 'state', 'workspace', 'evidence']) mkdirSync(resolve(root, name))
  const scope = { experiment_id: 'offline-pause', variant: 'chain-a', user_id: 'alice', session_id: 'main' }
  const localToken = 'offline-model-token-never-sent'
  const toolToken = 'offline-scope-token-never-sent'
  writeFileSync(resolve(configurationRoot, 'scope.token'), toolToken + '\n')
  writeFileSync(resolve(configurationRoot, 'scope.json'), JSON.stringify(scope))
  writeFileSync(resolve(root, 'workspace', 'request.json'), '{"offline":true}\n')
  const nativePath = resolve(root, 'state', 'native.jsonl')
  writeFileSync(nativePath, '{"offline":"native-bytes-only"}\n')
  const request = { run_id: 'chain-a', session_id: 'main', workspace: resolve(root, 'workspace'), state_dir: resolve(root, 'state'),
    output_dir: resolve(root, 'evidence'), tools_url: 'http://tools:8081', tool_token_file: '/config/scope.token',
    max_output_tokens: 4096, timeout_seconds: mode === 'new' ? 300 : 240, max_retries: 2,
    candidate_options: { case: 'tools', trusted_scope_file: '/config/scope.json' },
    lifecycle: { mode, chain_id: 'chain-a', attempt_id: mode === 'new' ? 'worker-a' : 'worker-b', binding_file: '/config/lifecycle.json' },
    ...(mode === 'new' ? { prompt: 'offline synthetic objective' } : {}) }
  const provenance = { source_export_sha256: 'a'.repeat(64), adapter_export_sha256: 'b'.repeat(64) }
  const image = 'sha256:' + 'c'.repeat(64)
  const env = { HARNESS_LIFECYCLE_IMAGE_ID: image, MODEL_API_BASE_URL: 'http://model-proxy:8082/v1', MODEL_NAME: 'offline-model' }
  const previous = Object.fromEntries(Object.keys(env).map(key => [key, process.env[key]]))
  Object.assign(process.env, env)
  t.after(() => { for (const [key, value] of Object.entries(previous)) value === undefined ? delete process.env[key] : process.env[key] = value; configurationRoot = undefined; rmSync(root, { recursive: true, force: true }) })
  const goal = { id: 'goal-native-a', revision: 2, objective: 'offline synthetic objective', phase: 'paused', maxGoalRounds: 6,
    roundsStarted: 1, createdAt: 10, updatedAt: 20, activation: 'disarmed' }
  const persisted = [
    { seq: 0, type: 'goal/change', time: 20, data: { operation: 'pause', goal: { id: goal.id, revision: 2, phase: 'paused' } } },
    { seq: 1, type: 'turn/end', time: 21, data: { turn: 1, reason: { kind: 'blocked' } } },
  ]
  const paused = { schema: 'harness-lab-goal-pause/1', candidate: 'deepseek_harness', chain_id: 'chain-a', scope: structuredClone(scope), session_id: 'main', goal,
    session: { id: 'main', cwd: request.workspace, next_seq: 2, artifact_sha256: sha256(readFileSync(nativePath)), persisted_events_sha256: objectHash(persisted) },
    source: { meaning: 'synthetic_adapter_calibration_not_framework_evidence' } }
  const binding = { schema: 'harness-lab-lifecycle/1', candidate: 'deepseek_harness', variant: 'goal-runtime', mode, chain_id: 'chain-a',
    attempt_id: request.lifecycle.attempt_id, scope: structuredClone(scope), session_id: 'main', materials: { ...provenance }, image: { original_image_id: image },
    model: { config_sha256: objectHash({ provider: 'deepseek-official', base_url: env.MODEL_API_BASE_URL, model: env.MODEL_NAME,
      max_output_tokens: 4096, max_retries: 2, thinking: 'disabled', reasoning_effort: 'off' }), run_token_sha256: sha256(localToken), budget_id: 'chain-a', max_model_calls: 20 },
    tools: { config_sha256: objectHash({ url: request.tools_url, scope, token_sha256: sha256(toolToken) }), contract_sha256: objectHash(sortedSchemas()) },
    state: { index_file: '/config/lifecycle-state.json', index_sha256: '' }, goal: { max_goal_rounds: 6 },
    active_budget: { limit_seconds: 300, used_seconds: mode === 'new' ? 0 : 60, remaining_seconds: mode === 'new' ? 300 : 240 },
    ...(mode === 'resume' ? { resume: { pause_index_file: '/config/lifecycle-pause.json', pause_index_sha256: '' } } : {}) }
  const saveJson = (name, value) => { const bytes = JSON.stringify(value) + '\n'; writeFileSync(resolve(configurationRoot, name), bytes); return sha256(bytes) }
  const saveBinding = () => saveJson('lifecycle.json', binding)
  const savePause = () => { if (binding.resume) binding.resume.pause_index_sha256 = saveJson('lifecycle-pause.json', paused); saveBinding() }
  const freezeState = () => { binding.state.index_sha256 = saveJson('lifecycle-state.json', stateInventory(request)); saveBinding() }
  savePause(); freezeState()
  const read = () => readGoalBinding(request, provenance, scope)
  return { root, request, scope, provenance, binding, paused, persisted, nativePath, localToken, toolToken, saveBinding, savePause, freezeState, read }
}

function noModel(t) {
  const sent = t.mock.method(globalThis, 'fetch', () => { throw new Error('适配器校准禁止访问网络或模型') })
  t.after(() => assert.equal(sent.mock.callCount(), 0))
}
function runtime(t, f, mode = f.request.lifecycle.mode) {
  const trusted = f.read()
  let current = mode === 'resume' ? structuredClone(f.paused.goal) : undefined
  const nativeCalls = { create: 0, resume: 0, pause: 0, followup: 0 }
  const hooks = new Map(), saved = new Map(), appended = []
  const events = mode === 'resume' ? [...structuredClone(f.persisted), { seq: 2, type: 'session/end-seed', time: 22, data: {} }] : []
  const agent = { id: 'main', status: 'idle', session: { header: { id: 'main', cwd: f.request.workspace }, seq: events.length, events },
    inbox: { hasPending: false, nextStep: [], nextTurn: [] }, ctx: { tools: { schemas: sortedSchemas } },
    whenIdle: async () => {}, followup() { nativeCalls.followup++; throw new Error('此路径中的适配器禁止追加后续输入') } }
  const ctx = { on(name, fn) { hooks.set(name, [...(hooks.get(name) ?? []), fn]); return () => {} },
    goals: { get: () => current,
      create(_agent, spec) { nativeCalls.create++; current = { ...f.paused.goal, revision: 1, roundsStarted: 0, objective: spec.objective, maxGoalRounds: spec.maxGoalRounds, phase: 'active', activation: 'armed' }; return current },
      resume() { nativeCalls.resume++; current = { ...current, revision: current.revision + 1, phase: 'active', activation: 'armed' }; return current },
      pause() { nativeCalls.pause++; current = { ...current, revision: current.revision + 1, phase: 'paused', activation: 'disarmed' }; emit('goal/change', { operation: 'pause', goal: current }); return current },
    } }
  const control = new GoalRuntime({ ...f.request, prompt: f.request.prompt ?? '' }, trusted)
  t.after(() => rmSync(control.runtimeHome, { recursive: true, force: true }))
  control.configure({ save(name, value) { saved.set(name, structuredClone(value)) }, append(name, value) { appended.push({ name, value }); return { path: name, line: appended.filter(row => row.name === name).length } } })
  control.install(ctx)
  function emit(type, data) {
    const event = { seq: events.length, type, time: 100 + events.length, data }
    events.push(event); agent.session.seq = events.length
    control.observe({ id: 'main' }, event, { path: 'raw-events.jsonl', line: events.length })
  }
  const before = () => ({ header: agent.session.header, next_seq: events.length, first_live_seq: mode === 'resume' ? f.persisted.length : 0,
    events: structuredClone(events), message_count: mode === 'resume' ? 1 : 0 })
  control.beforeResumeArtifact({ physical_sha256: f.paused.session.artifact_sha256 })
  const scoped = { on(name, fn) { hooks.set('scoped:' + name, [fn]) }, tools: { guard() {} } }
  return { control, agent, handle: { agent, dispose: async () => {} }, nativeCalls, before, events, ctx, hooks, saved, appended, emit,
    setGoal(value) { current = value }, currentGoal: () => current, scoped }
}
function silentClose(control, error) {
  const previous = process.exitCode
  const output = mock.method(process.stdout, 'write', () => true)
  try { control.afterClose(error) } finally { output.mock.restore(); process.exitCode = previous }
}

test('有效的新绑定可在不调用模型时核验令牌及重排后的原生结构', t => {
  noModel(t); const f = fixture(t), trusted = f.read()
  trusted.verifyToken(f.localToken)
  trusted.verifyToolContract([...schemas].reverse())
  assert.equal(trusted.binding.chain_id, f.scope.variant)
  assert.equal(trusted.image_verification.includes('host_must_verify_actual_docker_image_id'), true)
})

for (const [label, mutate, expected] of [
  ['scope', f => { f.binding.scope = { ...f.scope, user_id: 'bob' } }, /完整作用域不匹配/],
  ['chain', f => { f.binding.chain_id = 'chain-b' }, /完整作用域不匹配/],
  ['attempt', f => { f.binding.attempt_id = 'worker-b' }, /完整作用域不匹配/],
  ['source', f => { f.binding.materials = { ...f.binding.materials, source_export_sha256: 'd'.repeat(64) } }, /材料不匹配/],
  ['adapter', f => { f.binding.materials = { ...f.binding.materials, adapter_export_sha256: 'd'.repeat(64) } }, /材料不匹配/],
  ['image', f => { f.binding.image.original_image_id = 'sha256:' + 'd'.repeat(64) }, /镜像声明不匹配/],
  ['预算身份', f => { f.binding.model.budget_id = 'other-chain' }, /共享模型预算绑定/],
  ['模型调用上限', f => { f.binding.model.max_model_calls = 21 }, /共享模型预算绑定/],
  ['模型配置', f => { f.request.max_retries = 1 }, /模型配置不匹配/],
  ['工具作用域配置', f => { f.binding.tools.config_sha256 = 'd'.repeat(64) }, /工具配置/],
  ['耗尽的时间', f => { f.binding.active_budget.remaining_seconds = 0 }, /活跃时间预算/],
  ['被重置的总时长', f => { f.binding.active_budget.used_seconds = 1 }, /活跃时间预算/],
]) test('绑定在调用模型前拒绝错误项：' + label + '（模型调用前）', t => {
  noModel(t); const f = fixture(t); mutate(f); f.saveBinding(); assert.throws(f.read, expected)
})

test('错误的模型令牌或已修改的工具说明及参数不能使用有效绑定', t => {
  noModel(t); const f = fixture(t), trusted = f.read()
  assert.throws(() => trusted.verifyToken('other-token'), /运行令牌发生变化/)
  assert.throws(() => trusted.verifyToolContract([...schemas, { name: 'extra', description: 'x', parameters: {} }]), /接口发生变化/)
  const changed = structuredClone(schemas); changed[0].parameters.properties.value.type = 'number'
  assert.throws(() => trusted.verifyToolContract(changed), /接口发生变化/)
  const changedDescription = structuredClone(schemas); changedDescription[0].description += ' changed'
  assert.throws(() => trusted.verifyToolContract(changedDescription), /接口发生变化/)
})

for (const mutation of ['add', 'delete', 'bytes', 'index bytes', 'symlink', 'hardlink']) test('完整的原生状态及工作区清单拒绝变更：' + mutation, t => {
  noModel(t); const f = fixture(t)
  if (mutation === 'add') writeFileSync(resolve(f.request.state_dir, 'extra.jsonl'), '{}')
  if (mutation === 'delete') unlinkSync(f.nativePath)
  if (mutation === 'bytes') appendFileSync(f.nativePath, '{"tampered":true}\n')
  if (mutation === 'index bytes') appendFileSync(resolve(configurationRoot, 'lifecycle-state.json'), ' ')
  if (mutation === 'symlink') symlinkSync(f.nativePath, resolve(f.request.workspace, 'linked-native'))
  if (mutation === 'hardlink') linkSync(f.nativePath, resolve(f.request.workspace, 'linked-native'))
  assert.throws(f.read, /清单不匹配|绑定文件发生变化|符号链接|不支持的生命周期状态文件/)
})

test('恢复请求不接收 prompt 字段，拒绝原提示、继续指令、空串或 null 替代输入', t => {
  noModel(t); const f = fixture(t, 'resume')
  const validated = validateGoalRequest(f.request)
  assert.equal(validated.prompt, '')
  assert.equal(Object.hasOwn(f.request, 'prompt'), false)
  for (const prompt of ['', null, 'continue', 'repeat the original objective']) assert.throws(() => validateGoalRequest({ ...f.request, prompt }), /恢复请求禁止/)
  assert.throws(() => validateGoalRequest({ ...f.request, candidate_options: { ...f.request.candidate_options, session_action: 'resume', followup: 'continue' } }), /不接受旧版 followup/)
  assert.throws(() => validateGoalRequest({ ...f.request, candidate_options: { case: 'browser', trusted_scope_file: '/config/scope.json' } }), /配置|用例|字段/)
})

test('新任务拒绝缺失目标；恢复请求不能冒用 worker-a 或其他绑定挂载点', t => {
  noModel(t); const f = fixture(t)
  for (const prompt of ['', null, undefined]) assert.throws(() => validateGoalRequest({ ...f.request, prompt }), /长度受限的目标/)
  assert.throws(() => validateGoalRequest({ ...f.request, lifecycle: { ...f.request.lifecycle, mode: 'resume' } }), /生命周期请求无效/)
  assert.throws(() => validateGoalRequest({ ...f.request, lifecycle: { ...f.request.lifecycle, binding_file: '/workspace/lifecycle.json' } }), /生命周期请求无效/)
})

for (const [label, mutate] of [
  ['phase', p => { p.goal.phase = 'active' }], ['activation', p => { p.goal.activation = 'armed' }],
  ['round cap', p => { p.goal.maxGoalRounds = 7 }], ['exhausted rounds', p => { p.goal.roundsStarted = p.goal.maxGoalRounds }],
  ['scope', p => { p.scope.user_id = 'bob' }], ['session', p => { p.session.id = 'other' }],
]) test('恢复绑定拒绝被修改的暂停项：' + label, t => {
  noModel(t); const f = fixture(t, 'resume'); mutate(f.paused); f.savePause()
  assert.throws(f.read, /Goal 或作用域|冻结的轮数|Session 索引/)
})

test('恢复仅接收原持久化前缀及一个真实的结束种子标记，不追加新输入', async t => {
  noModel(t); const f = fixture(t, 'resume'), r = runtime(t, f)
  await r.control.start(r.handle, r.before())
  assert.deepEqual(r.nativeCalls, { create: 0, resume: 1, pause: 0, followup: 0 })
})

for (const [label, mutate] of [
  ['missing end-seed', before => { before.events.pop(); before.next_seq-- }],
  ['different end-seed type', before => { before.events.at(-1).type = 'turn/end' }],
  ['nonempty marker data', before => { before.events.at(-1).data = { extra: true } }],
  ['wrong seed boundary', before => { before.first_live_seq-- }],
  ['extra live event', before => { before.events.push({ seq: 3, type: 'turn/start', data: { turn: 2 } }); before.next_seq++ }],
  ['changed historical prefix', before => { before.events[0].data.goal.revision = 99 }],
]) test('原生恢复拒绝变更项：' + label + '（调用 goals.resume 或模型前）', async t => {
  noModel(t); const f = fixture(t, 'resume'), r = runtime(t, f), before = r.before(); mutate(before)
  await assert.rejects(r.control.start(r.handle, before), /原生恢复的 Goal\/Session 与可信暂停源不一致/)
  assert.equal(r.nativeCalls.resume, 0); assert.equal(r.nativeCalls.followup, 0)
})

test('授予原生恢复权限前检查恢复的 Goal 定义和原始产物', async t => {
  noModel(t); const f = fixture(t, 'resume'), r = runtime(t, f)
  r.setGoal({ ...f.paused.goal, maxGoalRounds: 7 })
  await assert.rejects(r.control.start(r.handle, r.before()), /原生恢复的 Goal\/Session 与可信暂停源不一致/)
  assert.equal(r.nativeCalls.resume, 0)
  r.setGoal(structuredClone(f.paused.goal)); r.control.beforeResumeArtifact({ physical_sha256: 'd'.repeat(64) })
  await assert.rejects(r.control.start(r.handle, r.before()), /原始 Session 字节/)
  assert.equal(r.nativeCalls.resume, 0)
})

test('实际工具面发生变化时，在创建可信 Goal 前失败', async t => {
  noModel(t); const f = fixture(t), r = runtime(t, f)
  r.agent.ctx.tools.schemas = () => [...schemas, { name: 'unexpected', description: '未冻结', parameters: {} }]
  await assert.rejects(r.control.start(r.handle, r.before()), /接口发生变化/)
  assert.equal(r.nativeCalls.create, 0)
})

async function safePoint(t) {
  const f = fixture(t), r = runtime(t, f)
  await r.control.setup(r.scoped); await r.control.start(r.handle, r.before())
  // 此处使用模拟的模型和工具进度；这些测试仅验证适配器检查，
  // 不用于验证 DSH 实际的公开事件生成或持久化实现。
  r.setGoal({ ...r.currentGoal(), roundsStarted: 1 }); r.agent.status = 'running'; r.emit('turn/start', { turn: 1 })
  return { f, r, preStep: r.hooks.get('scoped:agent/pre-step')[0] }
}

test('等待下游钩子期间收到暂停请求时，在进入新步骤前再次检查', async t => {
  noModel(t); const { r, preStep } = await safePoint(t)
  const decision = await preStep({ agent: r.agent, messages: [], turn: 1, step: 2, signal: new AbortController().signal }, async () => {
    await Promise.resolve(); r.control.onPauseSignal(); return { kind: 'enter', messages: [{ source: { kind: 'plugin' } }] }
  })
  assert.deepEqual(decision, { kind: 'reject' }); assert.equal(r.nativeCalls.pause, 1)
})

test('保留非空的原始输入领取结果，不能据此制造安全暂停', async t => {
  noModel(t); const { r, preStep } = await safePoint(t)
  r.control.onPauseSignal()
  const messages = [{ id: 'foreign-input', source: { kind: 'user' }, content: [] }]
  const decision = await preStep({ agent: r.agent, messages, turn: 1, step: 2, signal: new AbortController().signal }, async () => ({ kind: 'enter', messages }))
  assert.equal(decision.messages, messages); assert.equal(r.nativeCalls.pause, 0)
  assert.equal(r.appended.some(row => row.value.reason === 'original_claimed_messages_nonempty'), true)
})

test('空闲状态缺少匹配的真实 blocked turn/end 时不能算作暂停', async t => {
  noModel(t); const { f, r, preStep } = await safePoint(t)
  r.control.onPauseSignal(); await preStep({ agent: r.agent, messages: [], turn: 1, step: 2, signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [] }))
  r.agent.status = 'idle'
  assert.throws(() => r.control.afterIdle(r.handle, { physical_sha256: sha256(readFileSync(f.nativePath)), native_location: { path: f.nativePath } }), /匹配的 Goal 暂停/)
  assert.equal(r.saved.has('lifecycle-pause-candidate.json'), false)
})

test('正常关闭失败时不发布候选暂停索引或已暂停状态', async t => {
  noModel(t); const { f, r, preStep } = await safePoint(t)
  r.control.onPauseSignal(); await preStep({ agent: r.agent, messages: [], turn: 1, step: 2, signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [] }))
  r.emit('turn/end', { turn: 1, reason: { kind: 'blocked' } }); r.agent.status = 'idle'
  assert.equal(r.control.afterIdle(r.handle, { physical_sha256: sha256(readFileSync(f.nativePath)), native_location: { path: f.nativePath } }).kind, 'paused')
  r.control.recordResult({ status: 'pause_pending_close', native_status: 'incomplete', stop_reason: 'blocked' })
  silentClose(r.control, new Error('模拟保存器关闭失败'))
  assert.equal(r.saved.get('result.json').status, 'failed')
  assert.equal(r.saved.has('lifecycle-pause-candidate.json'), false)
  assert.equal(r.saved.has('native-state-index-after-close.json'), false)
})

test('关闭期间 Session 字节变化时不能发布持久暂停状态', async t => {
  noModel(t); const { f, r, preStep } = await safePoint(t)
  r.control.onPauseSignal(); await preStep({ agent: r.agent, messages: [], turn: 1, step: 2, signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [] }))
  r.emit('turn/end', { turn: 1, reason: { kind: 'blocked' } }); r.agent.status = 'idle'
  r.control.afterIdle(r.handle, { physical_sha256: sha256(readFileSync(f.nativePath)), native_location: { path: f.nativePath } })
  r.control.recordResult({ status: 'pause_pending_close' }); appendFileSync(f.nativePath, 'changed-after-flush\n')
  assert.throws(() => silentClose(r.control), /关闭期间发生变化/)
  assert.equal(r.saved.has('lifecycle-pause-candidate.json'), false)
})
