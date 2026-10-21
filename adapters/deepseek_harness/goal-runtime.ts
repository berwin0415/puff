/** I22 draft: public Goal lifecycle; no application-owned agent/task loop. */
import { mkdtempSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { canonicalJson, sha256, stateInventory } from './goal-binding.mjs'
import type { RunRequest } from './entrypoint.ts'
import type { Agent, AgentHandle, AgentSetup } from '../sources/main/packages/core/agent/src/index.ts'
import type { GoalView } from '../sources/main/packages/goal/goal/src/types.ts'
import type {} from '../sources/main/packages/goal/goal/src/index.ts'

type Context = Awaited<ReturnType<typeof import('../sources/main/apps/cli/src/profile-boot.ts')['runProfile']>>['ctx']

type Source = { path: string; line: number }
type IO = { append(name: string, value: unknown): Source; save(name: string, value: unknown): void }
type NativeEnd = { turn: number; reason: { kind: string }; seq: number; source: Source }
type PauseIntent = { id: string; turn: number; at: string; source: Source; goal?: GoalView; goalSource?: Source; end?: NativeEnd }
type Binding = ReturnType<typeof import('./goal-binding.mjs')['readGoalBinding']>

/** Only an explicitly selected, separately frozen Goal entry supplies this bridge. */
export class GoalRuntime {
  readonly variant = 'goal-runtime'
  readonly profile = 'harness-lab-goal'
  readonly runtimeHome = mkdtempSync('/tmp/harness-lab-goal-')
  readonly patchFiles = [resolve(import.meta.dirname, 'goal-runtime.cordis.patch.yml')]
  private io?: IO
  private ctx?: Context
  private agent?: Agent
  private inputActive = false
  private activeTurn?: number
  private lastEnd?: NativeEnd
  private intent?: PauseIntent
  private requestSeen = false
  private concurrentInput = false
  private nativeErrors = 0
  private inFlightModels = 0
  private inFlightTools = 0
  private expectedGoal?: { id: string; objective: string; maxGoalRounds: number }
  private artifact?: any
  private pauseIndex?: any
  private summary?: any
  private closed = false
  private controlSequence = 0
  private readonly stopped = Promise.withResolvers<void>()

  constructor(readonly request: RunRequest, readonly trusted: Binding) {}
  configure(io: IO) { this.io = io }
  pauseHandlerInstalled() {
    this.io!.append('control-events.jsonl', { scope: this.trusted.binding.scope, chain_id: this.request.lifecycle!.chain_id,
      attempt_id: this.request.lifecycle!.attempt_id, time: new Date().toISOString(), phase: 'pause_handler_installed', signal: 'SIGUSR2',
      action: 'pause', pause_confirmed: false, meaning: 'signal_handler_only_not_active_work_or_safe_pause' })
  }
  private append(phase: string, extra: Record<string, unknown> = {}) {
    return this.io!.append('pause-events.jsonl', {
      scope: this.trusted.binding.scope, chain_id: this.request.lifecycle!.chain_id,
      attempt_id: this.request.lifecycle!.attempt_id, time: new Date().toISOString(),
      signal: 'SIGUSR2', action: 'pause', phase, ...extra,
    })
  }
  async beforeBoot() {
    // Profiles/module links are reproducible process configuration in /tmp.
    // Only native sessions use the persistent /state mount.
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
      // This adapter never supplies ordinary followup/steer/inject input.
      // Official goal-round messages are the sole automatic task producer.
      const goal = ctx.goals.get(agent)
      if (message.source.kind !== 'goal' || message.source.round <= 0 || message.source.goalId !== goal?.id) this.concurrentInput = true
    })
    const self = this
    ctx.on('llm/stream', (options, next) => {
      if (String(options.sessionId) !== self.request.session_id) return next()
      return (async function* () {
        self.inFlightModels++
        try { yield* next() } finally { self.inFlightModels-- }
      })()
    })
  }
  setup: AgentSetup = async agentCtx => {
    agentCtx.on('tools/execute', async (_exec, next) => {
      this.inFlightTools++
      try { return await next() } finally { this.inFlightTools-- }
    })
    // Public monotonic guard freezes the trusted objective/round cap. Goal
    // complete/blocked still belongs to the model through the official tool.
    agentCtx.tools.guard(exec => {
      if (exec.name === 'create_goal') return 'i22_goal_already_created_by_trusted_control'
      if (exec.name === 'update_goal' && !['complete', 'blocked'].includes((exec.arguments as { action?: string }).action ?? '')) return 'i22_trusted_goal_spec_is_frozen'
      return undefined
    })
    agentCtx.on('agent/pre-step', async (payload, next) => {
      // Snapshot the original claim before any downstream middleware adds
      // runtime context. Never inspect decision.messages as the raw claim.
      const claimedCount = payload.messages.length
      if (this.tryPause(payload, claimedCount, 'before_next')) return { kind: 'reject' }
      const decision = await next()
      if (decision.kind === 'enter' && this.tryPause(payload, claimedCount, 'after_next')) return { kind: 'reject' }
      if (decision.kind === 'reject' && this.intent && !this.intent.goal) this.rejectIntent('downstream_rejected_step')
      return decision
    }, { prepend: true })
  }
  private rejectIntent(reason: string) {
    if (!this.intent) return
    this.append('not_paused', { request_id: this.intent.id, reason, request_source: this.intent.source,
      behavior: 'preserve_original_step_and_input_no_requeue_no_drop_no_cancel' })
    this.intent = undefined
  }
  private tryPause(payload: { agent: Agent; turn: number; step: number; signal: AbortSignal }, claimedCount: number, boundary: string): boolean {
    const intent = this.intent
    if (!intent || intent.goal) return false
    const agent = payload.agent
    const goal = this.ctx!.goals.get(agent)
    const refusal = payload.signal.aborted ? 'turn_already_aborted'
      : agent !== this.agent || payload.turn !== intent.turn ? 'different_turn_or_agent'
      : claimedCount !== 0 ? 'original_claimed_messages_nonempty'
      : agent.inbox.hasPending || this.concurrentInput ? 'pending_inbox_or_concurrent_input'
      : this.inFlightModels !== 0 || this.inFlightTools !== 0 ? 'native_calls_still_in_flight'
      : !goal || goal.id !== this.expectedGoal?.id || goal.objective !== this.expectedGoal.objective || goal.maxGoalRounds !== this.expectedGoal.maxGoalRounds ? 'goal_binding_changed'
      : goal.phase !== 'active' || goal.activation !== 'armed' ? 'goal_not_active_and_armed'
      : goal.roundsStarted >= goal.maxGoalRounds ? 'no_remaining_goal_rounds' : undefined
    if (refusal) { this.rejectIntent(refusal); return false }
    const paused = this.ctx!.goals.pause(agent, { id: goal!.id, revision: goal!.revision })
    intent.goal = paused
    this.append('public_goal_pause_returned', { request_id: intent.id, boundary, turn: payload.turn, proposed_step: payload.step,
      original_claimed_count: claimedCount, inbox_pending: agent.inbox.hasPending, goal: paused,
      native_api: 'goals.pause', next_action: 'return_original_public_pre_step_reject', request_source: intent.source })
    return true
  }
  onPauseSignal = () => {
    const status = this.agent?.status ?? 'no_handle'
    const invalid = this.closed ? 'attempt_closed' : !this.inputActive ? 'no_active_goal_input'
      : this.requestSeen ? 'duplicate_request' : status !== 'running' || this.activeTurn === undefined ? 'no_active_native_turn'
      : undefined
    if (invalid) { this.append('not_requested', { reason: invalid, native_status: status }); return }
    try {
      const goal = this.ctx!.goals.get(this.agent!)
      if (!goal || goal.phase !== 'active' || goal.activation !== 'armed') { this.append('not_requested', { reason: 'goal_not_active_and_armed', native_status: status }); return }
      this.requestSeen = true
      const id = this.request.run_id + ':' + this.request.lifecycle!.attempt_id + ':pause:' + (++this.controlSequence)
      const at = new Date().toISOString()
      const source = this.append('requested', { request_id: id, turn: this.activeTurn, requested_at: at, native_status: status,
        effect: 'intent_only_no_cancel_abort_dispose_or_private_mutation' })
      this.intent = { id, at, turn: this.activeTurn!, source }
    } catch (error) {
      this.append('control_error', { error: error instanceof Error ? error.message : String(error), pause_confirmed: false })
    }
  }
  observe(session: any, event: any, source: Source) {
    if (String(session.id) !== this.request.session_id) return
    if (event.type === 'turn/start') this.activeTurn = event.data.turn
    if (event.type === 'goal/change' && event.data.operation === 'pause' && this.intent) this.intent.goalSource = source
    if (event.type === 'turn/end') {
      this.lastEnd = { turn: event.data.turn, reason: event.data.reason, seq: event.seq, source }
      if (this.intent?.goal && event.data.turn === this.intent.turn) {
        this.intent.end = { turn: event.data.turn, reason: event.data.reason, seq: event.seq, source }
        this.append('native_turn_end_observed', { request_id: this.intent.id, turn: event.data.turn,
          native_turn_end_reason: event.data.reason, native_event_seq: event.seq, source,
          goal_change_source: this.intent.goalSource ?? null })
      }
      if (this.activeTurn === event.data.turn) this.activeTurn = undefined
    }
  }
  async start(handle: AgentHandle, before: any) {
    this.agent = handle.agent
    const ctx = this.ctx!
    this.trusted.verifyToolContract(handle.agent.ctx.tools.schemas(handle.agent))
    const goal = ctx.goals.get(handle.agent)
    if (this.request.lifecycle!.mode === 'resume') {
      const expected = this.trusted.paused!
      const storedCount = expected.session.next_seq
      const marker = before.events?.at(-1)
      const exactRestoredPrefix = Array.isArray(before.events)
        && before.first_live_seq === storedCount && before.next_seq === storedCount + 1 && before.events.length === storedCount + 1
        && marker?.seq === storedCount && marker?.type === 'session/end-seed' && canonicalJson(marker.data) === '{}'
        && sha256(canonicalJson(before.events.slice(0, storedCount))) === expected.session.persisted_events_sha256
      if (!goal || canonicalJson(goal) !== canonicalJson(expected.goal) || !exactRestoredPrefix || before.header.id !== expected.session.id || before.header.cwd !== expected.session.cwd) throw new Error('Native restored Goal/Session differs from the trusted paused source before model dispatch')
      if (this.artifact?.physical_sha256 !== expected.session.artifact_sha256) throw new Error('Restored raw Session bytes differ from trusted paused source')
      this.expectedGoal = { id: goal.id, objective: goal.objective, maxGoalRounds: goal.maxGoalRounds }
      this.inputActive = true
      const resumed = ctx.goals.resume(handle.agent, { id: goal.id, revision: goal.revision })
      this.append('native_goal_resume_returned', { native_api: 'goals.resume', input_type: 'persisted_goal_ref', goal: resumed,
        new_prompt_submitted: false, new_followup_submitted: false })
    } else {
      if (goal || before.message_count !== 0 || before.next_seq !== before.first_live_seq) throw new Error('New Goal variant requires a clean native Session with no prior messages')
      this.inputActive = true
      const created = ctx.goals.create(handle.agent, { objective: this.request.prompt, maxGoalRounds: this.trusted.binding.goal.max_goal_rounds })
      this.expectedGoal = { id: created.id, objective: created.objective, maxGoalRounds: created.maxGoalRounds }
      this.append('native_goal_create_returned', { native_api: 'goals.create', input_type: 'trusted_new_objective', goal: created,
        automatic_producer: 'official_goal_round_driver_only', new_followup_submitted: false })
    }
    this.considerStop()
  }
  beforeResumeArtifact(artifact: any) { this.artifact = artifact }
  private considerStop() {
    if (!this.inputActive || !this.agent || this.agent.status !== 'idle') return
    const goal = this.ctx!.goals.get(this.agent)
    // Ignore transient idle between official active/armed Goal rounds.
    if (!goal || goal.phase !== 'active' || goal.activation !== 'armed' || this.nativeErrors > 0) this.stopped.resolve()
  }
  async waitForStop(handle: AgentHandle) {
    await this.stopped.promise
    await handle.agent.whenIdle()
  }
  afterIdle(handle: AgentHandle, artifact: any) {
    this.inputActive = false
    const agent = handle.agent
    const goal = this.ctx!.goals.get(agent)
    if (agent.status !== 'idle' || agent.inbox.hasPending || this.inFlightModels || this.inFlightTools) throw new Error('Native Goal work has not reached a quiescent empty-inbox boundary')
    if (!goal || goal.id !== this.expectedGoal?.id || goal.objective !== this.expectedGoal.objective || goal.maxGoalRounds !== this.expectedGoal.maxGoalRounds) throw new Error('Final Goal identity/spec differs from its frozen binding')
    if (this.nativeErrors) throw new Error('Native agent/error observed; idle alone is not a successful pause or completion')
    const intent = this.intent
    if (intent?.goal) {
      if (this.concurrentInput || this.activeTurn !== undefined || goal.phase !== 'paused' || goal.activation !== 'disarmed' || !intent.goalSource || !intent.end || this.lastEnd?.seq !== intent.end.seq || canonicalJson(intent.end.reason) !== canonicalJson({ kind: 'blocked' }) || canonicalJson(goal) !== canonicalJson(intent.goal)) throw new Error('Missing actual matching Goal pause/blocked turn-end/paused-disarmed state')
      this.artifact = artifact
      this.pauseIndex = { schema: 'harness-lab-goal-pause/1', candidate: 'deepseek_harness', chain_id: this.request.lifecycle!.chain_id,
        scope: this.trusted.binding.scope, session_id: this.request.session_id, goal,
        session: { id: this.request.session_id, cwd: this.request.workspace, next_seq: agent.session.seq, artifact_sha256: artifact.physical_sha256,
          persisted_events_sha256: sha256(canonicalJson(agent.session.events)) },
        source: { pause_request: intent.source, goal_change: intent.goalSource, native_turn_end: intent.end.source,
          native_turn_end_seq: intent.end.seq, native_turn: intent.turn, flush: 'await_live_sessions_flush_after_native_turn_end_and_whenIdle',
          close: 'pending', host_acceptance: 'required_not_implied_by_candidate_index' } }
      this.append('native_idle_and_flush_observed', { request_id: intent.id, goal, native_status: agent.status,
        native_inbox_pending: false, in_flight_models: 0, in_flight_tools: 0, artifact,
        source: intent.end.source, pause_confirmed: false, reason: 'normal_close_and_host_checks_still_required' })
      return { kind: 'paused', native_reason: 'blocked', goal, pending_close: true }
    }
    return { kind: goal.phase === 'complete' && goal.activation === 'disarmed' && !this.requestSeen && this.activeTurn === undefined && this.lastEnd?.reason.kind === 'completed' ? 'completed' : 'incomplete', goal,
      reason: this.requestSeen ? 'pause_request_not_honored' : 'native_goal_not_complete' }
  }
  recordResult(summary: any) { this.summary = summary }
  afterClose(closeError?: unknown) {
    this.closed = true
    const result = this.summary ?? { status: 'failed', stop_reason: 'no_native_result', session_id: this.request.session_id }
    if (closeError) {
      result.status = 'failed'; result.stop_reason = 'native_close_failed'
      result.error = closeError instanceof Error ? closeError.message : String(closeError)
    } else if (result.status === 'pause_pending_close' && this.pauseIndex && this.artifact) {
      if (this.nativeErrors || this.inFlightModels || this.inFlightTools) throw new Error('Native failure or in-flight work observed during close')
      const bytes = readFileSync(this.artifact.native_location.path)
      if (sha256(bytes) !== this.artifact.physical_sha256) throw new Error('Native session artifact changed during close')
      this.pauseIndex.source.close = 'handle_dispose_private_scopes_and_runtime_shutdown_all_resolved'
      const inventory = stateInventory(this.request)
      this.io!.save('native-state-index-after-close.json', inventory)
      this.io!.save('lifecycle-pause-candidate.json', this.pauseIndex)
      result.status = 'paused'
      result.pause_confirmed = false
      result.pause_candidate_status = 'native_safe_point_flush_and_normal_close_observed_host_must_verify_full_chain'
      result.native_pause_index = 'lifecycle-pause-candidate.json'
      this.append('native_normal_close_observed', { native_api: 'AgentHandle.dispose + runtime.shutdown.shutdown',
        native_reason_preserved: 'blocked', pause_confirmed: false, host_validation_pending: true })
    } else if (result.status === 'goal_complete_pending_close') result.status = 'completed'
    result.lifecycle = { ...this.request.lifecycle, image_verification: this.trusted.image_verification }
    this.io!.save('result.json', result)
    process.stdout.write(JSON.stringify({ status: result.status, native_status: result.native_status, stop_reason: result.stop_reason }) + '\n')
    if (!['paused', 'completed'].includes(result.status)) process.exitCode = 1
  }
}
