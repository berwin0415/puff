/** Adapter boundary calibration only: built-in node:test, fake native APIs, no model/upstream boot. */
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
// Only translate the container mount alias. Production filesystem checks,
// no-follow opens, bytes, hashes and complete-tree inventories remain real.
mock.module(detailedURL.href, { namedExports: { ...detailed,
  checkedExistingPath: filename => detailed.checkedExistingPath(mapped(filename)),
  readTrustedJson: filename => detailed.readTrustedJson(mapped(filename)),
} })
const { canonicalJson, sha256, readGoalBinding, stateInventory, validateGoalRequest } = await import(new URL('goal-binding.mjs', adapter).href)
const { GoalRuntime } = await import(new URL('goal-runtime.ts', adapter).href)
const objectHash = value => sha256(canonicalJson(value))
const schemas = [
  { name: 'write', description: 'synthetic write schema', parameters: { type: 'object', properties: { value: { type: 'string' } } } },
  { name: 'get_goal', description: 'synthetic Goal schema', parameters: {} },
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
  const sent = t.mock.method(globalThis, 'fetch', () => { throw new Error('No network/model allowed in adapter calibration') })
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
    whenIdle: async () => {}, followup() { nativeCalls.followup++; throw new Error('Adapter must never followup in this route') } }
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

test('valid new binding verifies tokens and reordered native schema without invoking a model', t => {
  noModel(t); const f = fixture(t), trusted = f.read()
  trusted.verifyToken(f.localToken)
  trusted.verifyToolContract([...schemas].reverse())
  assert.equal(trusted.binding.chain_id, f.scope.variant)
  assert.equal(trusted.image_verification.includes('host_must_verify_actual_docker_image_id'), true)
})

for (const [label, mutate, expected] of [
  ['scope', f => { f.binding.scope = { ...f.scope, user_id: 'bob' } }, /full-scope mismatch/],
  ['chain', f => { f.binding.chain_id = 'chain-b' }, /full-scope mismatch/],
  ['attempt', f => { f.binding.attempt_id = 'worker-b' }, /full-scope mismatch/],
  ['source', f => { f.binding.materials = { ...f.binding.materials, source_export_sha256: 'd'.repeat(64) } }, /material mismatch/],
  ['adapter', f => { f.binding.materials = { ...f.binding.materials, adapter_export_sha256: 'd'.repeat(64) } }, /material mismatch/],
  ['image', f => { f.binding.image.original_image_id = 'sha256:' + 'd'.repeat(64) }, /image declaration mismatch/],
  ['budget identity', f => { f.binding.model.budget_id = 'other-chain' }, /shared model budget binding/],
  ['model cap', f => { f.binding.model.max_model_calls = 21 }, /shared model budget binding/],
  ['model config', f => { f.request.max_retries = 1 }, /model configuration mismatch/],
  ['tools scope config', f => { f.binding.tools.config_sha256 = 'd'.repeat(64) }, /tool configuration/],
  ['exhausted time', f => { f.binding.active_budget.remaining_seconds = 0 }, /active-time budget/],
  ['reset total time', f => { f.binding.active_budget.used_seconds = 1 }, /active-time budget/],
]) test('binding rejects wrong ' + label + ' before model dispatch', t => {
  noModel(t); const f = fixture(t); mutate(f); f.saveBinding(); assert.throws(f.read, expected)
})

test('wrong model token and changed tool descriptions/parameters cannot use a valid binding', t => {
  noModel(t); const f = fixture(t), trusted = f.read()
  assert.throws(() => trusted.verifyToken('other-token'), /run token changed/)
  assert.throws(() => trusted.verifyToolContract([...schemas, { name: 'extra', description: 'x', parameters: {} }]), /surface changed/)
  const changed = structuredClone(schemas); changed[0].parameters.properties.value.type = 'number'
  assert.throws(() => trusted.verifyToolContract(changed), /surface changed/)
  const changedDescription = structuredClone(schemas); changedDescription[0].description += ' changed'
  assert.throws(() => trusted.verifyToolContract(changedDescription), /surface changed/)
})

for (const mutation of ['add', 'delete', 'bytes', 'index bytes', 'symlink', 'hardlink']) test('complete native/workspace inventory rejects ' + mutation, t => {
  noModel(t); const f = fixture(t)
  if (mutation === 'add') writeFileSync(resolve(f.request.state_dir, 'extra.jsonl'), '{}')
  if (mutation === 'delete') unlinkSync(f.nativePath)
  if (mutation === 'bytes') appendFileSync(f.nativePath, '{"tampered":true}\n')
  if (mutation === 'index bytes') appendFileSync(resolve(configurationRoot, 'lifecycle-state.json'), ' ')
  if (mutation === 'symlink') symlinkSync(f.nativePath, resolve(f.request.workspace, 'linked-native'))
  if (mutation === 'hardlink') linkSync(f.nativePath, resolve(f.request.workspace, 'linked-native'))
  assert.throws(f.read, /inventory mismatch|bound file changed|symbolic links|Unsupported lifecycle state file/)
})

test('resume accepts no prompt field and rejects any original/continue/empty/null replacement', t => {
  noModel(t); const f = fixture(t, 'resume')
  const validated = validateGoalRequest(f.request)
  assert.equal(validated.prompt, '')
  assert.equal(Object.hasOwn(f.request, 'prompt'), false)
  for (const prompt of ['', null, 'continue', 'repeat the original objective']) assert.throws(() => validateGoalRequest({ ...f.request, prompt }), /Resume forbids/)
  assert.throws(() => validateGoalRequest({ ...f.request, candidate_options: { ...f.request.candidate_options, session_action: 'resume', followup: 'continue' } }), /without legacy followup/)
  assert.throws(() => validateGoalRequest({ ...f.request, candidate_options: { case: 'browser', trusted_scope_file: '/config/scope.json' } }), /configuration|case|fields/)
})

test('new rejects a missing objective; resume cannot claim worker-a or another binding mount', t => {
  noModel(t); const f = fixture(t)
  for (const prompt of ['', null, undefined]) assert.throws(() => validateGoalRequest({ ...f.request, prompt }), /bounded objective/)
  assert.throws(() => validateGoalRequest({ ...f.request, lifecycle: { ...f.request.lifecycle, mode: 'resume' } }), /Invalid lifecycle request/)
  assert.throws(() => validateGoalRequest({ ...f.request, lifecycle: { ...f.request.lifecycle, binding_file: '/workspace/lifecycle.json' } }), /Invalid lifecycle request/)
})

for (const [label, mutate] of [
  ['phase', p => { p.goal.phase = 'active' }], ['activation', p => { p.goal.activation = 'armed' }],
  ['round cap', p => { p.goal.maxGoalRounds = 7 }], ['exhausted rounds', p => { p.goal.roundsStarted = p.goal.maxGoalRounds }],
  ['scope', p => { p.scope.user_id = 'bob' }], ['session', p => { p.session.id = 'other' }],
]) test('resume binding rejects changed paused ' + label, t => {
  noModel(t); const f = fixture(t, 'resume'); mutate(f.paused); f.savePause()
  assert.throws(f.read, /Goal or scope|frozen rounds|Session index/)
})

test('resume accepts the exact persisted prefix plus one genuine end-seed marker, never a new followup', async t => {
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
]) test('native resume rejects ' + label + ' before goals.resume/model', async t => {
  noModel(t); const f = fixture(t, 'resume'), r = runtime(t, f), before = r.before(); mutate(before)
  await assert.rejects(r.control.start(r.handle, before), /restored Goal\/Session differs/)
  assert.equal(r.nativeCalls.resume, 0); assert.equal(r.nativeCalls.followup, 0)
})

test('restored Goal spec and raw artifact are checked before native resume authority', async t => {
  noModel(t); const f = fixture(t, 'resume'), r = runtime(t, f)
  r.setGoal({ ...f.paused.goal, maxGoalRounds: 7 })
  await assert.rejects(r.control.start(r.handle, r.before()), /restored Goal\/Session differs/)
  assert.equal(r.nativeCalls.resume, 0)
  r.setGoal(structuredClone(f.paused.goal)); r.control.beforeResumeArtifact({ physical_sha256: 'd'.repeat(64) })
  await assert.rejects(r.control.start(r.handle, r.before()), /raw Session bytes/)
  assert.equal(r.nativeCalls.resume, 0)
})

test('a changed actual surface fails before trusted Goal creation', async t => {
  noModel(t); const f = fixture(t), r = runtime(t, f)
  r.agent.ctx.tools.schemas = () => [...schemas, { name: 'unexpected', description: 'not frozen', parameters: {} }]
  await assert.rejects(r.control.start(r.handle, r.before()), /surface changed/)
  assert.equal(r.nativeCalls.create, 0)
})

async function safePoint(t) {
  const f = fixture(t), r = runtime(t, f)
  await r.control.setup(r.scoped); await r.control.start(r.handle, r.before())
  // Model/tool progress is synthetic here; these tests prove adapter checks,
  // not DSH's actual public event generation or persistence implementation.
  r.setGoal({ ...r.currentGoal(), roundsStarted: 1 }); r.agent.status = 'running'; r.emit('turn/start', { turn: 1 })
  return { f, r, preStep: r.hooks.get('scoped:agent/pre-step')[0] }
}

test('a pause received during awaited downstream hook is checked again before entering a new step', async t => {
  noModel(t); const { r, preStep } = await safePoint(t)
  const decision = await preStep({ agent: r.agent, messages: [], turn: 1, step: 2, signal: new AbortController().signal }, async () => {
    await Promise.resolve(); r.control.onPauseSignal(); return { kind: 'enter', messages: [{ source: { kind: 'plugin' } }] }
  })
  assert.deepEqual(decision, { kind: 'reject' }); assert.equal(r.nativeCalls.pause, 1)
})

test('nonempty original claims are preserved and cannot manufacture a safe pause', async t => {
  noModel(t); const { r, preStep } = await safePoint(t)
  r.control.onPauseSignal()
  const messages = [{ id: 'foreign-input', source: { kind: 'user' }, content: [] }]
  const decision = await preStep({ agent: r.agent, messages, turn: 1, step: 2, signal: new AbortController().signal }, async () => ({ kind: 'enter', messages }))
  assert.equal(decision.messages, messages); assert.equal(r.nativeCalls.pause, 0)
  assert.equal(r.appended.some(row => row.value.reason === 'original_claimed_messages_nonempty'), true)
})

test('idle without the matching real blocked turn/end cannot become a pause', async t => {
  noModel(t); const { f, r, preStep } = await safePoint(t)
  r.control.onPauseSignal(); await preStep({ agent: r.agent, messages: [], turn: 1, step: 2, signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [] }))
  r.agent.status = 'idle'
  assert.throws(() => r.control.afterIdle(r.handle, { physical_sha256: sha256(readFileSync(f.nativePath)), native_location: { path: f.nativePath } }), /matching Goal pause/)
  assert.equal(r.saved.has('lifecycle-pause-candidate.json'), false)
})

test('normal close failure never publishes a candidate pause index or paused status', async t => {
  noModel(t); const { f, r, preStep } = await safePoint(t)
  r.control.onPauseSignal(); await preStep({ agent: r.agent, messages: [], turn: 1, step: 2, signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [] }))
  r.emit('turn/end', { turn: 1, reason: { kind: 'blocked' } }); r.agent.status = 'idle'
  assert.equal(r.control.afterIdle(r.handle, { physical_sha256: sha256(readFileSync(f.nativePath)), native_location: { path: f.nativePath } }).kind, 'paused')
  r.control.recordResult({ status: 'pause_pending_close', native_status: 'incomplete', stop_reason: 'blocked' })
  silentClose(r.control, new Error('simulated saver close failure'))
  assert.equal(r.saved.get('result.json').status, 'failed')
  assert.equal(r.saved.has('lifecycle-pause-candidate.json'), false)
  assert.equal(r.saved.has('native-state-index-after-close.json'), false)
})

test('changed Session bytes during close cannot publish a durable pause', async t => {
  noModel(t); const { f, r, preStep } = await safePoint(t)
  r.control.onPauseSignal(); await preStep({ agent: r.agent, messages: [], turn: 1, step: 2, signal: new AbortController().signal }, async () => ({ kind: 'enter', messages: [] }))
  r.emit('turn/end', { turn: 1, reason: { kind: 'blocked' } }); r.agent.status = 'idle'
  r.control.afterIdle(r.handle, { physical_sha256: sha256(readFileSync(f.nativePath)), native_location: { path: f.nativePath } })
  r.control.recordResult({ status: 'pause_pending_close' }); appendFileSync(f.nativePath, 'changed-after-flush\n')
  assert.throws(() => silentClose(r.control), /changed during close/)
  assert.equal(r.saved.has('lifecycle-pause-candidate.json'), false)
})
