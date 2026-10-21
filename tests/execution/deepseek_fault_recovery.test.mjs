/** I23 adapter trust boundary; fake native APIs only, never boot or dispatch a model. */
import assert from 'node:assert/strict'
import { mkdtempSync, mkdirSync, realpathSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { mock, test } from 'node:test'

const adapter = process.env.DSH_FAULT_ADAPTER_SOURCE ? pathToFileURL(resolve(process.env.DSH_FAULT_ADAPTER_SOURCE) + '/') : new URL('../../adapters/deepseek_harness/', import.meta.url)
const detailedURL = new URL('detailed-options.mjs', adapter)
const detailed = await import(detailedURL.href)
let configRoot
const mapped = filename => String(filename).startsWith('/config/') ? resolve(configRoot, String(filename).slice('/config/'.length)) : filename
mock.module(detailedURL.href, { namedExports: { ...detailed,
  checkedExistingPath: filename => detailed.checkedExistingPath(mapped(filename)),
  readTrustedJson: filename => detailed.readTrustedJson(mapped(filename)),
} })
const { canonicalJson, sha256, readFaultGoalBinding, stateInventory, validateFaultGoalRequest } = await import(new URL('fault-goal-binding.mjs', adapter).href)
const { FaultGoalRuntime } = await import(new URL('fault-goal-runtime.ts', adapter).href)
const hash = value => sha256(canonicalJson(value))
const schemas = [{ name: 'write', description: 'synthetic', parameters: { type: 'object' } }]

function fixture(t) {
  const root = mkdtempSync(resolve(realpathSync(tmpdir()), 'dsh-fault-boundary-'))
  for (const dir of ['config', 'state', 'state/sessions', 'workspace', 'evidence']) mkdirSync(resolve(root, dir))
  configRoot = resolve(root, 'config')
  const scope = { experiment_id: 'fault-recovery', variant: 'chain-a', user_id: 'alice', session_id: 'main' }
  const request = { run_id: 'chain-a', session_id: 'main', workspace: resolve(root, 'workspace'), state_dir: resolve(root, 'state'),
    output_dir: resolve(root, 'evidence'), tools_url: 'http://tools:8081', tool_token_file: '/config/scope.token',
    max_output_tokens: 4096, timeout_seconds: 220, max_retries: 2,
    candidate_options: { case: 'tools', trusted_scope_file: '/config/scope.json' },
    fault_recovery: { mode: 'crash-resume', chain_id: 'chain-a', attempt_id: 'worker-b', binding_file: '/config/fault-goal.json' } }
  const provenance = { source_export_sha256: 'a'.repeat(64), adapter_export_sha256: 'b'.repeat(64) }
  const original = [
    { seq: 0, time: 10, type: 'turn/start', data: { turn: 1 } },
    { seq: 1, time: 11, type: 'tool/call', data: { callId: 'call-a', turn: 1, step: 1, name: 'write', arguments: '{}' } },
  ]
  // Synthetic closer fixture. This test checks only that the runtime refuses
  // altered public restored evidence, not that the native SDK produced it.
  const repair = [{ seq: 2, time: 11, type: 'turn/end', data: { turn: 1, reason: { kind: 'interrupted' } } }]
  const repaired = [...original, ...repair]
  const bytes = Buffer.from(JSON.stringify({ type: 'session', id: 'main', cwd: request.workspace }) + '\n' + original.map(row => JSON.stringify(row) + '\n').join(''))
  const physical = resolve(root, 'state/sessions/main.jsonl')
  writeFileSync(physical, bytes)
  writeFileSync(resolve(configRoot, 'scope.token'), 'offline-tool-token\n')
  writeFileSync(resolve(configRoot, 'scope.json'), JSON.stringify(scope))
  const goal = { id: 'original-goal', revision: 1, objective: 'original-objective', phase: 'active', maxGoalRounds: 20,
    roundsStarted: 1, createdAt: 10, updatedAt: 10 }
  const image = 'sha256:' + 'c'.repeat(64)
  const env = { HARNESS_I23_IMAGE_ID: image, MODEL_API_BASE_URL: 'http://model-proxy:8082/v1', MODEL_NAME: 'offline-model' }
  const previous = Object.fromEntries(Object.keys(env).map(key => [key, process.env[key]]))
  Object.assign(process.env, env)
  t.after(() => { for (const [key, value] of Object.entries(previous)) value === undefined ? delete process.env[key] : process.env[key] = value; rmSync(root, { recursive: true, force: true }); configRoot = undefined })
  const binding = { schema: 'harness-lab-dsh-crash-binding/1', candidate: 'deepseek_harness', variant: 'fault-goal-runtime',
    mode: 'crash-resume', chain_id: 'chain-a', attempt_id: 'worker-b', scope, session_id: 'main', materials: provenance,
    image: { original_image_id: image }, model: { budget_id: 'chain-a', max_model_calls: 20, run_token_sha256: sha256('same-token'),
      config_sha256: hash({ provider: 'deepseek-official', base_url: env.MODEL_API_BASE_URL, model: env.MODEL_NAME,
        max_output_tokens: 4096, max_retries: 2, thinking: 'disabled', reasoning_effort: 'off' }) },
    tools: { config_sha256: hash({ url: request.tools_url, scope, token_sha256: sha256('offline-tool-token') }), contract_sha256: hash(schemas) },
    goal: { max_goal_rounds: 20 }, active_budget: { limit_seconds: 300, used_seconds: 80, remaining_seconds: 220 },
    state: { index_file: '/config/fault-state.json', index_sha256: '' },
    recovery: { boundary_sha256: 'd'.repeat(64), audit_sha256: 'e'.repeat(64), physical_path: 'sessions/main.jsonl',
      physical_bytes: bytes.length, physical_sha256: sha256(bytes), committed_bytes: bytes.length,
      committed_prefix_sha256: sha256(bytes), events_sha256: hash(original), next_seq: original.length,
      expected_repaired_events_sha256: hash(repaired), expected_repaired_next_seq: repaired.length, durable_goal: goal } }
  const save = (name, value) => { const encoded = JSON.stringify(value) + '\n'; writeFileSync(resolve(configRoot, name), encoded); return sha256(encoded) }
  const saveBinding = () => save('fault-goal.json', binding)
  const freeze = () => { binding.state.index_sha256 = save('fault-state.json', stateInventory(request)); saveBinding() }
  freeze()
  const read = () => readFaultGoalBinding(validateFaultGoalRequest(request), provenance, scope)
  const network = t.mock.method(globalThis, 'fetch', () => { throw new Error('No model/network allowed during adapter calibration') })
  t.after(() => assert.equal(network.mock.callCount(), 0))
  return { request, scope, provenance, binding, goal, repaired, repair, bytes, physical, read, saveBinding, freeze }
}

test('crash binding pins original bytes, full scope, unchanged token and remaining budget', t => {
  const f = fixture(t), trusted = f.read()
  trusted.verifyToken('same-token')
  assert.throws(() => trusted.verifyToken('reset-token'), /run token changed/)
  trusted.verifyToolContract(schemas)
  assert.equal(trusted.recovery.durable_goal.id, 'original-goal')
})

for (const change of ['prompt', 'lifecycle', 'followup', 'old_binding', 'wrong_attempt']) test('crash request rejects ' + change, t => {
  const f = fixture(t)
  if (change === 'old_binding') f.request.fault_recovery.binding_file = '/config/lifecycle.json'
  else if (change === 'wrong_attempt') f.request.fault_recovery.attempt_id = 'worker-a'
  else f.request[change] = 'forbidden'
  assert.throws(() => validateFaultGoalRequest(f.request))
})

for (const change of ['scope', 'image', 'material', 'original_bytes', 'reset_budget', 'armed_inferred', 'completed_goal', 'path_escape', 'safe_pause']) test('binding rejects ' + change + ' before native dispatch', t => {
  const f = fixture(t)
  if (change === 'scope') f.binding.scope = { ...f.scope, user_id: 'bob' }
  if (change === 'image') f.binding.image.original_image_id = 'sha256:' + 'f'.repeat(64)
  if (change === 'material') f.binding.materials = { ...f.provenance, adapter_export_sha256: 'f'.repeat(64) }
  if (change === 'original_bytes') { writeFileSync(f.physical, Buffer.concat([f.bytes, Buffer.from('extra')])); f.freeze() }
  if (change === 'reset_budget') f.binding.active_budget.remaining_seconds = 300
  if (change === 'armed_inferred') f.binding.recovery.durable_goal.activation = 'armed'
  if (change === 'completed_goal') f.binding.recovery.durable_goal.phase = 'complete'
  if (change === 'path_escape') f.binding.recovery.physical_path = 'sessions/../../other.jsonl'
  if (change === 'safe_pause') { writeFileSync(resolve(f.request.state_dir, 'pause-index.json'), '{}'); f.freeze() }
  f.saveBinding()
  assert.throws(f.read)
})

function runtime(t, f) {
  const control = new FaultGoalRuntime({ ...f.request, prompt: '' }, f.read())
  t.after(() => rmSync(control.runtimeHome, { recursive: true, force: true }))
  const saved = new Map(), appended = [], handlers = new Map(), calls = { resume: 0, create: 0, followup: 0 }
  const on = (name, callback) => { const listeners = handlers.get(name) ?? []; listeners.push(callback); handlers.set(name, listeners) }
  const emit = (name, ...args) => { for (const callback of handlers.get(name) ?? []) callback(...args) }
  control.configure({ save(name, value) { saved.set(name, structuredClone(value)) }, append(_name, value) {
    appended.push(structuredClone(value)); return { path: 'fault-events.jsonl', line: appended.length }
  } })
  // Inject a fake prepared public native view without invoking any upstream
  // boot. Source-level/native repair calibration is a separate Docker check.
  control.repairedEvents = structuredClone(f.repaired)
  control.repairEvents = structuredClone(f.repair)
  control.restoredHeader = { id: 'main', cwd: f.request.workspace, version: 1, createdAt: 10, delegationDepth: 0 }
  const agent = { id: 'main', status: 'idle', ctx: { tools: { schemas: () => schemas } },
    inbox: { hasPending: false }, followup() { calls.followup++; throw new Error('No followup allowed') } }
  const ctx = { on, goals: { get: () => ({ ...f.goal, activation: f.goal.activation ?? 'disarmed' }),
    resume(_agent, ref) { calls.resume++; assert.deepEqual(ref, { id: f.goal.id, revision: f.goal.revision }); return { ...f.goal, revision: 2, activation: 'armed' } },
    create() { calls.create++; throw new Error('No replacement Goal allowed') } } }
  control.install(ctx)
  control.beforeResumeArtifact({ physical_sha256: sha256(f.bytes), physical_bytes: f.bytes.length, scope: f.scope })
  const events = [...structuredClone(f.repaired), { seq: f.repaired.length, time: 30, type: 'session/end-seed', data: {} }]
  const before = { header: structuredClone(control.restoredHeader), agent_status: 'idle', inbox: { has_pending: false },
    first_live_seq: f.repaired.length, next_seq: events.length, events }
  return { control, handle: { agent }, before, calls, saved, appended, on, emit, handlers }
}

test('exact public restored prefix reuses only the original Goal ref without new user input', async t => {
  const f = fixture(t), r = runtime(t, f)
  await r.control.start(r.handle, r.before)
  assert.deepEqual(r.calls, { resume: 1, create: 0, followup: 0 })
  const proof = r.saved.get('native-crash-resume.json')
  assert.equal(proof.pause_index_used, false)
  assert.equal(proof.new_prompt_submitted, false)
  assert.deepEqual(proof.repair_events, f.repair)
})

for (const change of ['prefix', 'repair', 'marker', 'pending', 'live_seq', 'goal', 'header_metadata', 'header_delegation', 'header_default_missing']) test('runtime refuses altered restored ' + change + ' before original Goal resumes', async t => {
  const f = fixture(t), r = runtime(t, f)
  if (change === 'prefix') r.before.events[0].data.turn = 9
  if (change === 'repair') r.before.events[2].data.reason.kind = 'completed'
  if (change === 'marker') r.before.events.at(-1).type = 'turn/start'
  if (change === 'pending') r.before.inbox.has_pending = true
  if (change === 'live_seq') r.before.first_live_seq--
  if (change === 'goal') f.goal.id = 'replacement'
  if (change === 'header_metadata') r.before.header.origin = 'replacement-metadata'
  if (change === 'header_delegation') r.before.header.delegationDepth = 1
  if (change === 'header_default_missing') delete r.before.header.delegationDepth
  await assert.rejects(r.control.start(r.handle, r.before))
  assert.deepEqual(r.calls, { resume: 0, create: 0, followup: 0 })
})

/** Exercise only public observer contracts and pure official notice renderers. */
async function wrapup(t, action = 'complete', change) {
  const f = fixture(t), r = runtime(t, f)
  await r.control.start(r.handle, r.before)
  await r.control.setup({ on: r.on, tools: { guard() {} } })
  const { createUserMessage, boundContextSummary } = await import(new URL('../sources/main/packages/llm/llm/src/message.ts', adapter).href)
  const { renderWrapupContext } = await import(new URL('../sources/main/packages/goal/tool-goal/src/wrapup.ts', adapter).href)
  Object.assign(f.goal, { activation: 'armed', roundsStarted: action === 'blocked' ? 2 : 1 })
  const agent = r.handle.agent
  agent.status = 'running'
  const round = createUserMessage({ content: [{ type: 'text', text: 'synthetic next goal round' }],
    source: { kind: 'goal', goalId: f.goal.id, revision: f.goal.revision, round: f.goal.roundsStarted + 1 } })
  r.emit('agent/inbox/inserted', { agent, message: round })
  const args = { goal_id: f.goal.id, revision: f.goal.revision, action,
    ...(action === 'blocked' ? { blocked_reason: 'synthetic model-reported blocker' } : {}) }
  const source = seq => ({ path: 'raw-events.jsonl', line: seq + 1 })
  const observe = event => r.control.observe({ id: 'main' }, event, source(event.seq))
  observe({ seq: 8, type: 'turn/start', data: { turn: 2 } })
  // Native user/message admission, not inbox insertion, advances this counter.
  if (change !== 'unadmitted_goal_round') {
    f.goal.roundsStarted = round.source.round
    observe({ seq: 9, type: 'user/message', data: round })
  }
  if (change !== 'no_native_call') observe({ seq: 11, type: 'tool/call', data: { turn: 2, step: 1,
    callId: 'goal-call', name: 'update_goal', arguments: JSON.stringify(change === 'different_native_arguments' ? { ...args, revision: 99 } : args) } })
  const exec = { callId: 'goal-call', name: 'update_goal', arguments: { ...args }, agent }
  if (change === 'wrong_goal') exec.arguments.goal_id = 'foreign-goal'
  if (change === 'wrong_revision') exec.arguments.revision = 99
  if (change === 'foreign_agent') exec.agent = { id: 'main' }
  if (change === 'other_tool') exec.name = 'write'
  const notice = createUserMessage({ source: { kind: 'plugin', plugin: 'tool-goal', form: 'notice',
    summary: boundContextSummary(`${action}: ${f.goal.objective}`) },
    content: renderWrapupContext(f.goal.objective, args.blocked_reason) })
  let result
  await r.handlers.get('tools/execute')[0](exec, async () => {
    Object.assign(f.goal, { revision: 2, phase: action === 'complete' ? 'complete' : 'blocked', activation: 'disarmed',
      ...(action === 'blocked' ? { blockedReason: { code: 'model-reported', message: args.blocked_reason } } : {}) })
    const value = { goal: { id: f.goal.id, revision: f.goal.revision, objective: f.goal.objective, phase: f.goal.phase,
      roundsStarted: f.goal.roundsStarted, maxGoalRounds: f.goal.maxGoalRounds,
      ...(f.goal.blockedReason ? { blockedReason: f.goal.blockedReason } : {}) }, activation: 'disarmed' }
    result = { isError: false, value, content: [{ type: 'text', text: JSON.stringify(value) }], additionalContexts: [notice] }
    return result
  })
  if (change === 'failed_tool') result = { ...result, isError: true, value: undefined }
  if (change === 'wrong_result_goal') result.value = { ...result.value, goal: { ...result.value.goal, id: 'foreign-goal' } }
  if (change === 'extra_notice') result.additionalContexts = [notice, notice]
  if (change === 'forged_notice_content') result.additionalContexts = [{ ...notice, content: [{ type: 'text', text: 'new work disguised as notice' }] }]
  if (change === 'forged_notice_source') result.additionalContexts = [{ ...notice, source: { ...notice.source, summary: 'forged completion' } }]
  if (change !== 'no_public_result') r.emit('tools/result', change === 'different_execution' ? { ...exec } : exec, result)
  if (change === 'before_commit') r.emit('agent/inbox/inserted', { agent, message: notice })
  observe({ seq: 12, type: 'tool/result', sourceEventSeqs: change === 'wrong_call_source' ? [99] : [11], data: {
    turn: 2, step: 1, message: { source: { kind: 'tool', callId: change === 'wrong_result_call' ? 'other-call' : 'goal-call' },
      content: [{ type: 'tool-result', toolCallId: 'goal-call', isError: change === 'native_tool_error', content: result.content }] } } })
  let inserted = result.additionalContexts[0]
  if (change === 'altered_after_result') inserted = { ...notice, content: [{ type: 'text', text: 'changed after public result' }] }
  if (change === 'different_message_id') inserted = { ...notice, id: 'replacement-message-id' }
  if (change === 'external_followup') inserted = createUserMessage({ content: notice.content, source: { kind: 'user' } })
  if (change === 'other_plugin') inserted = { ...notice, source: { ...notice.source, plugin: 'foreign-plugin' } }
  r.emit('agent/inbox/inserted', { agent, message: inserted })
  if (change === 'duplicate_delivery') r.emit('agent/inbox/inserted', { agent, message: inserted })
  observe({ seq: 13, type: 'turn/end', data: { turn: 2, reason: { kind: 'completed' } } })
  agent.status = 'idle'
  const outcome = r.control.afterIdle(r.handle, { physical_sha256: 'd'.repeat(64) })
  return { ...r, outcome, terminal: r.saved.get('native-fault-terminal.json') }
}

for (const action of ['complete', 'blocked']) test('official same-Goal ' + action + ' wrap-up is accepted once after its actual tool result', async t => {
  const r = await wrapup(t, action)
  assert.equal(r.terminal.concurrent_input, false)
  assert.equal(r.outcome.kind, action === 'complete' ? 'completed' : 'incomplete')
  assert.deepEqual(r.calls, { resume: 1, create: 0, followup: 0 })
  const accepted = r.appended.filter(row => row.phase === 'native_goal_wrapup_notice_accepted')
  assert.equal(accepted.length, 1)
  assert.equal(accepted[0].call_id, 'goal-call')
  assert.equal(accepted[0].goal_id, 'original-goal')
  assert.equal(accepted[0].action, action)
  assert.equal(accepted[0].tool_call_source.line, 12)
  assert.equal(accepted[0].tool_result_source.line, 13)
})

for (const change of ['wrong_goal', 'wrong_revision', 'foreign_agent', 'other_tool', 'no_native_call', 'different_native_arguments',
  'failed_tool', 'wrong_result_goal', 'extra_notice', 'forged_notice_content', 'forged_notice_source', 'no_public_result',
  'different_execution', 'before_commit', 'wrong_call_source', 'wrong_result_call', 'native_tool_error',
  'altered_after_result', 'different_message_id', 'external_followup', 'other_plugin', 'duplicate_delivery', 'unadmitted_goal_round']) {
  test('wrap-up correlation preserves concurrent-input rejection for ' + change, async t => {
    const r = await wrapup(t, 'complete', change)
    assert.equal(r.terminal.concurrent_input, true)
    assert.equal(r.outcome.kind, 'incomplete')
  })
}

// Safe projection of the preserved, ended real baseline. No prompt, reasoning,
// model content or credential is copied. This ordering failed before the fix:
// insertion compared round=1 with roundsStarted=0 and poisoned the whole run.
const observedBaselineRound = {
  evidence: '2026-10-20/deepseek_harness/fault-recovery/i23-dsh-observed-baseline-101-none-id-101/attempts/worker-a/candidate/raw-events.jsonl',
  create: { seq: 0, goalId: 'goal-c85ac561-d168-49f8-be88-23cedad5c3af', revision: 1, roundsStarted: 0 },
  inserted: { seq: 1, message: { id: '2b04dfe2-b57c-42f9-9cf2-12cbae7bce43',
    source: { kind: 'goal', goalId: 'goal-c85ac561-d168-49f8-be88-23cedad5c3af', revision: 1, round: 1 } } },
  admitted: { seq: 5, messageId: '2b04dfe2-b57c-42f9-9cf2-12cbae7bce43', roundsStarted: 1 },
}

test('preserved real baseline queues round 1 while roundsStarted is still 0 without concurrent input', t => {
  const f = fixture(t), r = runtime(t, f), observed = observedBaselineRound
  // Only the observer contract is replayed; this is not a native boot/recovery
  // claim. Real initial creation and eventual admission remain in raw evidence.
  r.control.agent = r.handle.agent
  Object.assign(f.goal, { id: observed.create.goalId, revision: observed.create.revision,
    roundsStarted: observed.create.roundsStarted, activation: 'armed' })
  r.emit('agent/inbox/inserted', { agent: r.handle.agent, message: observed.inserted.message })
  assert.equal(f.goal.roundsStarted, 0)
  assert.equal(r.control.concurrentInput, false, observed.evidence)
  assert.deepEqual(r.control.goalRound, {
    goalId: observed.create.goalId, revision: 1, round: 1,
  })
  assert.equal(observed.admitted.messageId, observed.inserted.message.id)
  assert.ok(observed.admitted.seq > observed.inserted.seq)
  f.goal.roundsStarted = observed.admitted.roundsStarted
  r.control.observe({ id: 'main' }, { seq: observed.admitted.seq, type: 'user/message',
    data: observed.inserted.message }, { path: 'raw-events.jsonl', line: observed.admitted.seq + 1 })
  assert.equal(r.control.concurrentInput, false)
})

for (const change of ['current_round', 'skipped_round', 'foreign_goal', 'foreign_revision', 'disarmed_goal', 'terminal_goal', 'over_round_limit']) {
  test('official Goal inbox reservation rejects ' + change, t => {
    const f = fixture(t), r = runtime(t, f)
    r.control.agent = r.handle.agent
    Object.assign(f.goal, { activation: 'armed', roundsStarted: 1 })
    const message = { id: 'queued-round', source: { kind: 'goal', goalId: f.goal.id, revision: f.goal.revision, round: 2 } }
    if (change === 'current_round') message.source.round = 1
    if (change === 'skipped_round') message.source.round = 3
    if (change === 'foreign_goal') message.source.goalId = 'foreign-goal'
    if (change === 'foreign_revision') message.source.revision++
    if (change === 'disarmed_goal') f.goal.activation = 'disarmed'
    if (change === 'terminal_goal') f.goal.phase = 'complete'
    if (change === 'over_round_limit') f.goal.maxGoalRounds = 1
    r.emit('agent/inbox/inserted', { agent: r.handle.agent, message })
    assert.equal(r.control.concurrentInput, true)
    assert.equal(r.control.goalRound, undefined)
  })
}
