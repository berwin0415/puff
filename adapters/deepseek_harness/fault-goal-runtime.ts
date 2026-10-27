/** I23：由官方 agents.resume 修复，再重新激活原始持久化 Goal。 */
import { mkdtempSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { canonicalJson, sha256, stateInventory } from './fault-goal-binding.mjs'
import type { RunRequest } from './entrypoint.ts'
import type { Agent, AgentHandle, AgentSetup } from '../sources/main/packages/core/agent/src/index.ts'
import type {} from '../sources/main/packages/goal/goal/src/index.ts'

type Context = Awaited<ReturnType<typeof import('../sources/main/apps/cli/src/profile-boot.ts')['runProfile']>>['ctx']
type Source = { path: string; line: number }
type IO = { append(name: string, value: unknown): Source; save(name: string, value: unknown): void }
export type FaultRunRequest = Omit<RunRequest, 'lifecycle'> & { fault_recovery: {
  mode: 'new' | 'crash-resume'; chain_id: string; attempt_id: 'worker-a' | 'worker-b'; binding_file: '/config/fault-goal.json'
} }
type Binding = ReturnType<typeof import('./fault-goal-binding.mjs')['readFaultGoalBinding']>
const objectHash = (value: unknown) => sha256(canonicalJson(value))
type GoalCall = { seq: number; turn: number; step: number; arguments_sha256: string; source: Source }
type GoalNotice = { call_id: string; call: GoalCall; goal_id: string; revision: number; phase: string;
  action: string; message_sha256: string; content_sha256: string; committed?: Source }

export class FaultGoalRuntime {
  readonly variant = 'fault-goal-runtime'
  readonly profile = 'harness-lab-fault-goal'
  readonly runtimeHome = mkdtempSync('/tmp/harness-lab-fault-goal-')
  readonly patchFiles = [resolve(import.meta.dirname, 'fault-goal-runtime.cordis.patch.yml')]
  private io?: IO
  private ctx?: Context
  private agent?: Agent
  private inputActive = false
  private activeTurn?: number
  private lastEnd?: { seq: number; data: any; source: Source }
  private nativeErrors = 0
  private inFlightModels = 0
  private inFlightTools = 0
  private concurrentInput = false
  private goalRound?: { goalId: string; revision: number; round: number }
  private goalCalls = new Map<string, GoalCall>()
  private goalNotices = new Map<string, GoalNotice>()
  private expectedGoal?: { id: string; objective: string; maxGoalRounds: number }
  private repairedEvents?: any[]
  private restoredHeader?: any
  private repairEvents: any[] = []
  private beforeArtifact?: any
  private summary?: any
  private terminal?: any
  private readonly stopped = Promise.withResolvers<void>()
  constructor(readonly request: FaultRunRequest, readonly trusted: Binding) {}
  configure(io: IO) { this.io = io }
  private append(phase: string, extra: Record<string, unknown> = {}) {
    return this.io!.append('fault-events.jsonl', { scope: this.trusted.binding.scope,
      chain_id: this.request.fault_recovery.chain_id, attempt_id: this.request.fault_recovery.attempt_id,
      time: new Date().toISOString(), phase, ...extra })
  }
  async beforeBoot() {
    if (this.request.fault_recovery.mode === 'crash-resume') {
      const recovery = this.trusted.recovery!
      const bytes = readFileSync(resolve(this.request.state_dir, recovery.physical_path))
      if (bytes.length !== recovery.physical_bytes || sha256(bytes) !== recovery.physical_sha256) throw new Error('原生解析前故障源字节发生变化')
      // 两个函数都是公开的原生源码纯函数 API，仅计算
      // 预期视图；不得将这些事件传入 prepare/import/append。
      // 实际持久化修复仅由下方的 agents.resume 执行。
      const { scanLog } = await import('../sources/main/packages/session/session-persistence-jsonl/src/format.ts')
      const { interruptedTurnClosers } = await import('../sources/main/packages/core/session/src/index.ts')
      const scanned = scanLog(bytes)
      if (String(scanned.meta.id) !== this.request.session_id || scanned.meta.cwd !== this.request.workspace
        || scanned.committedBytes !== recovery.committed_bytes || scanned.events.length !== recovery.next_seq
        || objectHash(scanned.events) !== recovery.events_sha256) throw new Error('原生解析器与独立核查的原始持久化前缀不一致')
      this.restoredHeader = scanned.meta
      this.repairEvents = interruptedTurnClosers(scanned.events)
      this.repairedEvents = [...scanned.events, ...this.repairEvents]
      if (this.repairedEvents.length !== recovery.expected_repaired_next_seq
        || objectHash(this.repairedEvents) !== recovery.expected_repaired_events_sha256) throw new Error('原生中断轮次修复与固定源码语义不一致')
      this.io!.save('native-crash-prepare.json', { scope: this.trusted.binding.scope,
        chain_id: this.request.run_id, original_physical_sha256: recovery.physical_sha256,
        committed_bytes: scanned.committedBytes, torn_tail_bytes: bytes.length - scanned.committedBytes,
        original_events_sha256: objectHash(scanned.events), original_next_seq: scanned.events.length,
        expected_repaired_events_sha256: objectHash(this.repairedEvents), repair_events: this.repairEvents,
        source: '仅使用公开纯函数 scanLog 和 interruptedTurnClosers；不修改状态或刷盘',
        execution_source: 'original_state_mount_via_agents.resume_not_this_expected_view',
        pause_index_used: false, new_prompt_submitted: false })
    }
    process.env.HARNESS_GOAL_SESSION_ROOT = resolve(this.request.state_dir, 'sessions')
    const { initProfile } = await import('../sources/main/packages/boot/app-boot/src/profile.ts')
    initProfile(resolve(this.runtimeHome, 'profiles', this.profile), ['@deepseek-ai/dsh-sdk-minimal'], 'startup')
  }
  install(ctx: Context) {
    this.ctx = ctx
    ctx.on('agent/status', ({ agent }) => { if (agent === this.agent) this.considerStop() })
    ctx.on('goal/changed', ({ agent }) => { if (agent === this.agent) this.considerStop() })
    ctx.on('agent/error', ({ agent }) => { if (agent === this.agent) { this.nativeErrors++; this.considerStop() } })
    ctx.on('agent/inbox/inserted', ({ agent, message }) => {
      if (agent !== this.agent) return
      const goal = ctx.goals.get(agent)
      if (message.source.kind === 'goal' && goal && message.source.goalId === goal.id
        && goal.phase === 'active' && goal.activation === 'armed'
        && message.source.revision === goal.revision && message.source.round === goal.roundsStarted + 1
        && message.source.round > 0 && message.source.round <= goal.maxGoalRounds) {
        // 官方驱动器先将下一轮排队，再由 user/message
        // 放行并推进 roundsStarted；下方 tools/execute 仍要求
        // 排队轮次与实际放行轮次相同。
        this.goalRound = { goalId: message.source.goalId, revision: message.source.revision, round: message.source.round }
        return
      }
      // 官方 tool-goal 收尾属于延迟上下文，不是新任务。
      // 仅放行同一 Goal 成功更新后精确匹配且只投递一次的消息，
      // 且对应的原生 tool/result 必须已提交。
      const notice = this.goalNotices.get(message.id)
      if (notice?.committed && objectHash(message) === notice.message_sha256
        && goal?.id === notice.goal_id && goal.revision === notice.revision
        && goal.phase === notice.phase && goal.activation === 'disarmed') {
        this.goalNotices.delete(message.id)
        this.append('native_goal_wrapup_notice_accepted', { native_api: 'tools/result -> tool/result -> agent/inbox/inserted',
          call_id: notice.call_id, goal_id: notice.goal_id, goal_revision: notice.revision, action: notice.action,
          tool_call_source: notice.call.source, tool_result_source: notice.committed,
          notice_message_id: message.id, notice_sha256: notice.message_sha256 })
        return
      }
      this.concurrentInput = true
    })
    const self = this
    ctx.on('llm/stream', (options, next) => {
      if (String(options.sessionId) !== self.request.session_id) return next()
      return (async function* () { self.inFlightModels++; try { yield* next() } finally { self.inFlightModels-- } })()
    })
  }
  setup: AgentSetup = async agentCtx => {
    // 官方纯渲染器保持固定版本通知约定完全一致；
    // 不产生新指令、工具结果、状态或延迟上下文。
    const { boundContextSummary } = await import('../sources/main/packages/llm/llm/src/message.ts')
    const { renderWrapupContext } = await import('../sources/main/packages/goal/tool-goal/src/wrapup.ts')
    const updates = new Map<string, { exec: object; before: any; args: any; call: GoalCall }>()
    agentCtx.on('tools/execute', async (exec, next) => {
      const before = this.agent && this.ctx?.goals.get(this.agent), args = exec.arguments as any
      const call = this.goalCalls.get(String(exec.callId))
      if (exec.agent === this.agent && exec.name === 'update_goal' && before && this.expectedGoal
        && ['complete', 'blocked'].includes(args?.action) && args.goal_id === before.id && args.revision === before.revision
        && before.id === this.expectedGoal.id && before.objective === this.expectedGoal.objective
        && before.maxGoalRounds === this.expectedGoal.maxGoalRounds && before.phase === 'active' && before.activation === 'armed'
        && this.goalRound?.goalId === before.id && this.goalRound.revision === before.revision
        && this.goalRound.round === before.roundsStarted && call && call.turn === this.activeTurn
        && call.arguments_sha256 === objectHash(args)) {
        updates.set(String(exec.callId), { exec, before: structuredClone(before), args, call })
      }
      this.inFlightTools++; try { return await next() } finally { this.inFlightTools-- }
    })
    agentCtx.on('tools/result', (exec, result) => {
      const update = updates.get(String(exec.callId))
      updates.delete(String(exec.callId))
      this.goalCalls.delete(String(exec.callId))
      if (!update || update.exec !== exec || exec.agent !== this.agent || result.isError
        || result.additionalContexts?.length !== 1) return
      const goal = this.ctx!.goals.get(this.agent!), value = result.value as any
      const phase = update.args.action === 'complete' ? 'complete' : 'blocked'
      if (!goal || goal.id !== update.before.id || goal.revision !== update.before.revision + 1
        || goal.objective !== update.before.objective || goal.maxGoalRounds !== update.before.maxGoalRounds
        || goal.roundsStarted !== update.before.roundsStarted || goal.phase !== phase || goal.activation !== 'disarmed') return
      if (phase === 'blocked' && (goal.blockedReason?.code !== 'model-reported'
        || goal.blockedReason.message !== update.args.blocked_reason)) return
      const expectedValue = { goal: { id: goal.id, revision: goal.revision, objective: goal.objective, phase: goal.phase,
        roundsStarted: goal.roundsStarted, maxGoalRounds: goal.maxGoalRounds,
        ...(goal.blockedReason === undefined ? {} : { blockedReason: goal.blockedReason }) }, activation: goal.activation }
      if (!value || typeof value !== 'object' || objectHash(value) !== objectHash(expectedValue)) return
      const message = result.additionalContexts[0]
      if (!message) return
      const expected = { id: message.id, role: 'user', source: { kind: 'plugin', plugin: 'tool-goal', form: 'notice',
        summary: boundContextSummary(`${update.args.action}: ${goal.objective}`) },
        content: renderWrapupContext(goal.objective, phase === 'blocked' ? update.args.blocked_reason : undefined) }
      if (typeof message.id !== 'string' || !message.id || this.goalNotices.has(message.id)
        || objectHash(message) !== objectHash(expected)) return
      this.goalNotices.set(message.id, { call_id: String(exec.callId), call: update.call, goal_id: goal.id,
        revision: goal.revision, phase, action: update.args.action, message_sha256: objectHash(message),
        content_sha256: objectHash(result.content) })
    })
    agentCtx.tools.guard(exec => {
      if (exec.name === 'create_goal') return 'i23_original_goal_owned_by_trusted_control'
      if (exec.name === 'update_goal' && !['complete', 'blocked'].includes((exec.arguments as { action?: string }).action ?? '')) return 'i23_original_goal_spec_is_frozen'
      return undefined
    })
  }
  observe(session: any, event: any, source: Source) {
    if (String(session.id) !== this.request.session_id) return
    if (event.type === 'tool/call' && event.data.name === 'update_goal') {
      try {
        const args = JSON.parse(event.data.arguments)
        this.goalCalls.set(event.data.callId, { seq: event.seq, turn: event.data.turn, step: event.data.step,
          arguments_sha256: objectHash(args), source })
      } catch { /* 无效原生参数不能授权收尾。 */ }
    }
    if (event.type === 'tool/result') {
      const block = event.data.message?.content?.[0]
      for (const notice of this.goalNotices.values()) {
        if (event.data.message?.source?.kind === 'tool' && event.data.message.source.callId === notice.call_id
          && event.data.turn === notice.call.turn && event.data.step === notice.call.step
          && Array.isArray(event.sourceEventSeqs) && event.sourceEventSeqs.length === 1 && event.sourceEventSeqs[0] === notice.call.seq
          && block?.type === 'tool-result' && block.toolCallId === notice.call_id && block.isError === false
          && Array.isArray(block.content) && objectHash(block.content) === notice.content_sha256) notice.committed = source
      }
    }
    if (event.type === 'turn/start') this.activeTurn = event.data.turn
    if (event.type === 'turn/end') {
      this.lastEnd = { seq: event.seq, data: event.data, source }
      if (this.activeTurn === event.data.turn) this.activeTurn = undefined
    }
  }
  beforeResumeArtifact(artifact: any) {
    this.beforeArtifact = artifact
    if (artifact.physical_sha256 !== this.trusted.recovery!.physical_sha256
      || artifact.physical_bytes !== this.trusted.recovery!.physical_bytes) throw new Error('恢复前公开的原始产物与原始故障字节不一致')
  }
  async start(handle: AgentHandle, before: any) {
    this.agent = handle.agent
    const ctx = this.ctx!
    this.trusted.verifyToolContract(handle.agent.ctx.tools.schemas(handle.agent))
    const goal = ctx.goals.get(handle.agent)
    if (this.request.fault_recovery.mode === 'crash-resume') {
      const recovery = this.trusted.recovery!
      const expected = this.repairedEvents!
      // 仅当传入的持久化历史尚未以 end-seed 标记结束时，
      // 原生 Session 才追加该标记；其时间来自原生进程。
      const needsMarker = expected.at(-1)?.type !== 'session/end-seed'
      const marker = before.events?.at(-1)
      if (!Array.isArray(before.events) || before.first_live_seq !== expected.length
        || before.next_seq !== expected.length + Number(needsMarker)
        || before.events.length !== before.next_seq
        || canonicalJson(before.events.slice(0, expected.length)) !== canonicalJson(expected)
        || (needsMarker && (marker?.seq !== expected.length || marker?.type !== 'session/end-seed' || canonicalJson(marker.data) !== '{}'))
        || canonicalJson(before.header) !== canonicalJson(this.restoredHeader)
        || before.agent_status !== 'idle' || before.inbox.has_pending
        || !this.beforeArtifact || canonicalJson(goal) !== canonicalJson({ ...recovery.durable_goal, activation: 'disarmed' })) throw new Error('实际 agents.resume 未恢复修复后的原始 Session 和已解除激活的原始 Goal')
      this.io!.save('native-crash-resume.json', { schema: 'harness-lab-dsh-crash-resume/1', scope: this.trusted.binding.scope,
        chain_id: this.request.run_id, session_id: this.request.session_id,
        boundary_sha256: recovery.boundary_sha256, audit_sha256: recovery.audit_sha256,
        native_api: 'agents.resume -> sessionPersistence.prepare native commitRepair -> goals.resume',
        original_artifact: this.beforeArtifact, restored_events_sha256: objectHash(before.events),
        original_prefix_sha256: objectHash(before.events.slice(0, recovery.next_seq)),
        repair_events: before.events.slice(recovery.next_seq, expected.length), restored_goal: goal,
        restored_session_source: 'session-before-followup.json',
        uncertainty: this.repairEvents.filter(event => event.type === 'tool/result').map(event => ({
          native_call_id: event.data.message.source.callId, error: event.data.error,
          source_event_seqs: event.sourceEventSeqs ?? [], repair_seq: event.seq,
          meaning: event.data.error?.code === 'TOOL_OUTCOME_UNKNOWN' ? '原生工具结果未知；不代表自动恢复业务' : '未记录原生工具开始事件；不代表工具未执行',
        })),
        recovery_granularity: '原生中断轮次修复后的原始持久化 Goal；不是任意指令位置',
        pause_index_used: false, new_prompt_submitted: false, new_followup_submitted: false })
      this.expectedGoal = { id: goal!.id, objective: goal!.objective, maxGoalRounds: goal!.maxGoalRounds }
      this.inputActive = true
      const resumed = ctx.goals.resume(handle.agent, { id: goal!.id, revision: goal!.revision })
      this.append('native_original_goal_resume_returned', { native_api: 'goals.resume', goal: resumed,
        original_goal_id: recovery.durable_goal.id, original_goal_revision: recovery.durable_goal.revision,
        new_prompt_submitted: false, new_followup_submitted: false, pause_index_used: false })
    } else {
      if (goal || before.message_count !== 0 || before.next_seq !== before.first_live_seq) throw new Error('新故障 Goal 必须使用干净的原始 Session')
      this.inputActive = true
      const created = ctx.goals.create(handle.agent, { objective: this.request.prompt, maxGoalRounds: this.trusted.binding.goal.max_goal_rounds })
      this.expectedGoal = { id: created.id, objective: created.objective, maxGoalRounds: created.maxGoalRounds }
      this.append('native_goal_create_returned', { native_api: 'goals.create', goal: created,
        automatic_producer: 'official_goal_round_driver_only', new_followup_submitted: false })
    }
    this.considerStop()
  }
  private considerStop() {
    if (!this.inputActive || !this.agent || this.agent.status !== 'idle') return
    const goal = this.ctx!.goals.get(this.agent)
    if (!goal || goal.phase !== 'active' || goal.activation !== 'armed' || this.nativeErrors > 0) this.stopped.resolve()
  }
  async waitForStop(handle: AgentHandle) { await this.stopped.promise; await handle.agent.whenIdle() }
  afterIdle(handle: AgentHandle, artifact: any) {
    this.inputActive = false
    if (this.goalNotices.size) this.concurrentInput = true
    const agent = handle.agent, goal = this.ctx!.goals.get(agent)
    if (agent.status !== 'idle' || agent.inbox.hasPending || this.inFlightModels || this.inFlightTools) throw new Error('故障 Goal 尚未完全静止')
    if (!goal || goal.id !== this.expectedGoal?.id || goal.objective !== this.expectedGoal.objective
      || goal.maxGoalRounds !== this.expectedGoal.maxGoalRounds) throw new Error('最终故障 Goal 的身份或目标发生变化')
    const complete = !this.nativeErrors && !this.concurrentInput && this.activeTurn === undefined
      && goal.phase === 'complete' && goal.activation === 'disarmed' && this.lastEnd?.data.reason.kind === 'completed'
    this.terminal = { schema: 'harness-lab-dsh-fault-terminal/1', scope: this.trusted.binding.scope,
      chain_id: this.request.run_id, session_id: this.request.session_id, goal,
      kind: complete ? 'completed' : 'incomplete', native_errors: this.nativeErrors, concurrent_input: this.concurrentInput,
      last_native_turn_end: this.lastEnd ?? null, artifact,
      repair_uncertainties: this.repairEvents.filter(event => event.type === 'tool/result').map(event => ({
        call_id: event.data.message.source.callId, error: event.data.error, seq: event.seq })),
      business_recovery_verified_by_candidate: false,
      meaning: '仅记录原生终止事实；必须独立核查业务账本并评价重放行为' }
    this.io!.save('native-fault-terminal.json', this.terminal)
    return { kind: complete ? 'completed' : 'incomplete', goal, native_errors: this.nativeErrors }
  }
  recordResult(summary: any) { this.summary = summary }
  afterClose(closeError?: unknown) {
    const result = this.summary ?? { status: 'failed', stop_reason: 'no_native_result', session_id: this.request.session_id }
    if (closeError) {
      result.status = 'failed'; result.stop_reason = 'native_close_failed'
      result.error = closeError instanceof Error ? closeError.message : String(closeError)
    } else if (result.status === 'goal_complete_pending_close') {
      if (this.nativeErrors || this.inFlightModels || this.inFlightTools) throw new Error('关闭故障 Goal 时存在原生失败或在途工作')
      result.status = 'completed'
    }
    result.fault_recovery = { ...this.request.fault_recovery, image_verification: this.trusted.image_verification }
    this.io!.save('native-state-index-after-close.json', stateInventory(this.request))
    this.io!.save('result.json', result)
    this.append('native_normal_close_observed', { native_api: 'AgentHandle.dispose + runtime.shutdown.shutdown',
      normal_close: !closeError, native_terminal_kind: this.terminal?.kind ?? 'missing' })
    process.stdout.write(JSON.stringify({ status: result.status, native_status: result.native_status, stop_reason: result.stop_reason }) + '\n')
    if (result.status !== 'completed') process.exitCode = 1
  }
}
