/** I23 isolated copy of the scoped native runtime. No pause/flush-before-kill path. */
import { appendFileSync, closeSync, constants, fstatSync, openSync, readFileSync, writeFileSync } from 'node:fs'
import { createHash } from 'node:crypto'
import { resolve, sep } from 'node:path'
import type { FaultRunRequest as RunRequest } from './fault-goal-runtime.ts'
import { checkedExistingPath, copyNativeDownload, nativeDownloadPaths, readBrowserCredentials, readTrustedScope, selectedSessionInput } from './detailed-options.mjs'
import { mountHttpTools } from './tools-plugin.mjs'
import { BUSINESS_EVENT, BUSINESS_META_KIND, BUSINESS_STATE_POINTER, BUSINESS_PROJECTION, mountBusinessExtension } from './business-extension.mjs'

const BROWSER_TOOLS = Object.freeze([
  'browser_navigate', 'browser_navigate_back', 'browser_snapshot',
  'browser_click', 'browser_fill_form', 'browser_type', 'browser_press_key',
  'browser_select_option', 'browser_wait_for', 'browser_tabs', 'browser_handle_dialog',
])
const REQUIRED_BROWSER_TOOLS = ['browser_navigate', 'browser_snapshot', 'browser_click', 'browser_fill_form']
const PREFIX = 'mcp__browser__'
type CancelControl = { requestId: string; requestedAt: string; turn: number; nativeStatusBefore: string;
  apiExecuted: boolean; requestSource: { path: string; line: number }; apiSource?: { path: string; line: number };
  turnEndReason?: any; turnEndSource?: { path: string; line: number }; idleLogged?: boolean }

export async function runScopedRuntime(request: RunRequest, provenance: unknown, lifecycle?: import('./fault-goal-runtime.ts').FaultGoalRuntime) {
  const options = request.candidate_options!
  const sessionAction = lifecycle ? (request.fault_recovery.mode === 'crash-resume' ? 'resume' : 'create') : options.session_action ?? 'create'
  const scope = readTrustedScope(options.trusted_scope_file, request.session_id)
  const paths = [request.workspace, request.state_dir, request.output_dir]
  if (paths.some((path, i) => paths.some((other, j) => i !== j && path.startsWith(other + '/')))) throw new Error('Detailed state, workspace and evidence trees must not overlap')
  const credentials = options.browser ? readBrowserCredentials(options.browser.credentials_file) : undefined
  if (credentials && credentials.username !== scope.user_id) throw new Error('Browser credentials do not match the trusted user identity')
  if (options.browser) checkedExistingPath(options.browser.artifacts_dir)
  const secrets = [process.env.MODEL_API_KEY, credentials?.password, readFileSync(checkedExistingPath(request.tool_token_file), 'utf8').trim()].filter(Boolean) as string[]
  const scrub = (value: unknown): string => {
    let encoded = JSON.stringify(value)
    for (const secret of secrets) {
      // Redact JSON-escaped values as well as strings appearing verbatim.
      encoded = encoded.replaceAll(JSON.stringify(secret).slice(1, -1), '[redacted-secret]')
    }
    return encoded
  }
  const save = (name: string, value: unknown) => writeFileSync(resolve(request.output_dir, name), scrub(value) + '\n')
  const writeNativeRecord = (name: string, exactRecord: string) => {
    if (name !== 'native-business-state-record.jsonl' || scrub(JSON.parse(exactRecord)) !== JSON.stringify(JSON.parse(exactRecord))) throw new Error('Native business record cannot be exported without changing secret bytes')
    writeFileSync(resolve(request.output_dir, name), exactRecord)
  }
  const lineCounts = new Map<string, number>()
  const append = (name: string, value: unknown) => {
    appendFileSync(resolve(request.output_dir, name), scrub(value) + '\n')
    const line = (lineCounts.get(name) ?? 0) + 1
    lineCounts.set(name, line)
    return { path: name, line }
  }
  lifecycle?.configure({ append, save })
  const started = Date.now()
  let timedOut = false
  let denied = false
  let copiedDownload: { native_path: string; source_file: string; destination: string; bytes: number; sha256: string } | undefined
  let deadlineTimer: ReturnType<typeof setTimeout> | undefined
  let hardStopTimer: ReturnType<typeof setTimeout> | undefined
  const controller = new AbortController()
  let runtime: Awaited<ReturnType<typeof import('../sources/main/apps/cli/src/profile-boot.ts')['runProfile']>> | undefined
  let handle: import('../sources/main/packages/core/agent/src/index.ts').AgentHandle | undefined
  let businessExtension: Awaited<ReturnType<typeof mountBusinessExtension>> | undefined
  const privateScopes: { dispose(): Promise<void> }[] = []
  const events: any[] = []
  let nativeFinalResponse = ''
  let inputSubmitted = false
  let activeTurn: number | undefined
  let activeTurnSource: { path: string; line: number } | undefined
  const controls: CancelControl[] = []
  let controlSequence = 0
  const recordIdleControls = (stage: string) => {
    for (const control of controls) {
      if (!control.apiExecuted || control.idleLogged || handle?.agent.status !== 'idle') continue
      control.idleLogged = true
      const reason = control.turnEndReason
      const confirmed = reason?.kind === 'aborted' && reason.reason?.kind === 'hook' && reason.reason?.reason === 'experiment_cancel'
      append('control-events.jsonl', { scope, time: new Date().toISOString(), request_id: control.requestId,
        requested_at: control.requestedAt, signal: 'SIGUSR1', action: 'cancel', phase: 'native_idle_observed', stage,
        native_api: 'Agent.cancel', api_executed: true, cancel_confirmed: confirmed,
        native_status_before: control.nativeStatusBefore, native_status_after: handle.agent.status,
        active_input_submitted: inputSubmitted, turn: control.turn, native_turn_end_reason: reason ?? null,
        confirmation_reason: confirmed ? 'matching_native_aborted_turn_and_idle' : reason ? 'different_native_turn_end' : 'missing_native_turn_end',
        native_inbox_pending: handle.agent.inbox.hasPending, source: control.turnEndSource,
        request_source: control.requestSource, api_source: control.apiSource })
    }
  }
  const onCancelSignal = () => {
    const requestedAt = new Date().toISOString()
    const requestId = request.run_id + ':cancel:' + (++controlSequence)
    const statusBefore = handle?.agent.status ?? 'no_handle'
    const base = { scope, request_id: requestId, requested_at: requestedAt, signal: 'SIGUSR1', action: 'cancel',
      native_api: 'Agent.cancel', native_status_before: statusBefore, active_input_submitted: inputSubmitted,
      turn: activeTurn ?? null, cause: { kind: 'hook', reason: 'experiment_cancel' }, source: activeTurnSource }
    let apiAttempted = false
    let apiExecuted = false
    try {
      const inactiveReason = !handle ? 'no_active_handle' : !inputSubmitted ? 'no_submitted_input'
        : activeTurn === undefined ? 'no_active_turn' : statusBefore !== 'running' ? 'native_agent_not_running' : undefined
      if (inactiveReason) {
        append('control-events.jsonl', { ...base, time: requestedAt, phase: 'not_cancelled', reason: inactiveReason,
          api_executed: false, cancel_confirmed: false, native_status_after: handle?.agent.status ?? 'no_handle' })
        return
      }
      const requestSource = append('control-events.jsonl', { ...base, time: requestedAt, phase: 'requested', api_executed: false,
        cancel_confirmed: false, native_status_after: handle!.agent.status })
      const control: CancelControl = { requestId, requestedAt, turn: activeTurn!, nativeStatusBefore: statusBefore, apiExecuted: false, requestSource }
      controls.push(control)
      apiAttempted = true
      handle!.agent.cancel({ kind: 'hook', reason: 'experiment_cancel' })
      control.apiExecuted = true
      apiExecuted = true
      control.apiSource = append('control-events.jsonl', { ...base, time: new Date().toISOString(), phase: 'native_api_returned',
        api_executed: true, cancel_confirmed: false, native_status_after: handle!.agent.status, request_source: requestSource })
    } catch (error) {
      // A signal handler must not turn an audit/API exception into a fake
      // cancellation or an unhandled process crash.
      const failure = { ...base, time: new Date().toISOString(), phase: 'control_error', cancel_confirmed: false,
        api_attempted: apiAttempted, api_executed: apiExecuted,
        native_status_after: handle?.agent.status ?? 'no_handle', error: error instanceof Error ? error.message : String(error) }
      try { append('control-events.jsonl', failure) } catch { process.stderr.write(scrub(failure) + '\n') }
    }
  }
  save('entrypoint.json', {
    run_id: request.run_id, session_id: request.session_id, scope, provenance,
    variant: lifecycle?.variant ?? 'scoped-runtime', profile: lifecycle?.profile ?? 'sdk-minimal', case: options.case,
    runtime_api: 'official runProfile + agents.create/agents.resume({setup})',
    session_action: sessionAction,
    resume_input: lifecycle ? (sessionAction === 'resume' ? 'original_native_goal_ref_no_new_prompt' : null) : sessionAction === 'resume' ? (options.followup === undefined ? 'inspection_only' : 'explicit_new_followup') : null,
    business_extension: options.business_extension ? {
      quantity_limit: options.business_extension.quantity_limit, added_tool: 'query_order_status -> receipt',
      native_state_event: BUSINESS_EVENT, native_projection_key: BUSINESS_PROJECTION,
      native_state_metadata_kind: BUSINESS_META_KIND, native_state_pointer: BUSINESS_STATE_POINTER,
      state_api: 'canonical tool output -> public output.presentationMeta -> native tool/result + sessionProjections.register/stateOf + sessions.flush',
      quantity_guard: 'public scoped tools.guard before actual HTTP write',
    } : null,
    browser: options.browser ? { url: options.browser.url, allowed_tools: BROWSER_TOOLS, credentials: 'separate_trusted_file', download_destination: 'result.csv' } : null,
    controls: { pause: 'not_used_in_crash_experiment', resume: lifecycle ? 'native_session_repair_then_same_goal_resume' : 'native_session_cold_load_only_unverified', cancel: 'SIGUSR1_native_agent_cancel_for_active_turn_and_deadline_cancel' },
    started_at: new Date(started).toISOString(),
  })
  process.env.DSH_HOME = lifecycle?.runtimeHome ?? request.state_dir
  process.env.DSH_TELEMETRY_DISABLED = '1'
  process.env.DSH_TOOLS_MODE = 'native'
  process.env.HARNESS_TOOLS_URL = request.tools_url
  process.env.HARNESS_TOOL_TOKEN_FILE = request.tool_token_file
  process.env.HARNESS_MAX_OUTPUT_TOKENS = String(request.max_output_tokens)
  process.env.HARNESS_MODEL_MAX_RETRIES = String(request.max_retries)
  process.chdir(request.workspace)
  process.on('SIGUSR1', onCancelSignal)

  const task = async () => {
    const { runProfile } = await import('../sources/main/apps/cli/src/profile-boot.ts')
    const { createLaunchEnvironmentSnapshot } = await import('../sources/main/packages/util/launch-environment/src/index.ts')
    if (lifecycle) await lifecycle.beforeBoot()
    runtime = await runProfile({
      environment: createLaunchEnvironmentSnapshot([{ source: 'process', values: process.env as Record<string, string> }]),
      profile: lifecycle?.profile ?? 'sdk-minimal', args: [],
      patchFiles: [resolve(import.meta.dirname, 'cordis.patch.yml'), resolve(import.meta.dirname, 'scoped-runtime.cordis.patch.yml'), ...(lifecycle?.patchFiles ?? [])],
    })
    if (controller.signal.aborted) throw new Error('Detailed task deadline expired during runtime startup')
    const ctx = runtime.ctx
    const { SessionId } = await import('../sources/main/packages/core/session/src/types.ts')
    const { createUserMessage } = await import('../sources/main/packages/llm/llm/src/index.ts')
    const { createScope } = await import('../sources/main/packages/core/scope/src/index.ts')
    const McpClient = await import('../sources/main/packages/mcp/mcp-client/src/index.ts')
    const persistence = ctx.get('sessionPersistence')
    if (!persistence?.supportsRawArtifacts) throw new Error('The minimal session probe requires the native raw-artifact persistence API')
    const sessionId = SessionId(request.session_id)
    const assertSessionIdentity = (header: { id: unknown; cwd?: string }) => {
      if (String(header.id) !== request.session_id || header.cwd !== request.workspace) throw new Error('Native session identity/cwd does not match the trusted request')
    }
    const captureArtifact = async (stage: string) => {
      const raw = await persistence.readRaw(sessionId, controller.signal)
      if (!raw) throw new Error('No native persisted session artifact exists')
      assertSessionIdentity(raw.meta)
      const location = persistence.locate(raw.meta)
      if (!location || location.kind !== 'jsonl') throw new Error('Expected a native JSONL artifact location')
      const filename = checkedExistingPath(location.path)
      if (!filename.startsWith(checkedExistingPath(request.state_dir) + sep)) throw new Error('Native session artifact escapes its trusted state mount')
      const descriptor = openSync(filename, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK)
      let bytes: Buffer
      try {
        const info = fstatSync(descriptor)
        if (!info.isFile() || info.size > 64 * 1024 * 1024) throw new Error('Expected a bounded native session artifact')
        bytes = readFileSync(descriptor)
      } finally { closeSync(descriptor) }
      const artifact = {
        stage, scope, header: raw.meta, native_location: location,
        physical_bytes: bytes.length, physical_sha256: createHash('sha256').update(bytes).digest('hex'),
        decoded_artifact_name: raw.filename, decoded_artifact_bytes: Buffer.byteLength(raw.content),
        decoded_artifact_sha256: createHash('sha256').update(raw.content).digest('hex'),
        source: 'public_sessionPersistence_readRaw_and_locate_plus_actual_file_bytes',
      }
      save('session-artifact-' + stage + '.json', artifact)
      if (stage === 'after-flush' && businessExtension && handle) businessExtension.exportNativeState(handle.agent.session, raw, artifact, sessionAction === 'resume')
      return artifact
    }
    const captureSession = (agent: import('../sources/main/packages/core/agent/src/index.ts').Agent, stage: string) => {
      const session = agent.session
      const messages = session.deriveMessages()
      const snapshot = {
        stage, scope, session_action: sessionAction, header: session.header,
        next_seq: session.seq, last_seq: session.seq - 1, first_live_seq: session.firstLiveSeq,
        events: session.events, events_sha256: createHash('sha256').update(JSON.stringify(session.events)).digest('hex'),
        messages, message_count: messages.length, agent_status: agent.status,
        inbox: { has_pending: agent.inbox.hasPending, next_turn: agent.inbox.nextTurn, next_step: agent.inbox.nextStep },
        tool_names: agent.ctx.tools.schemas(agent).map(tool => tool.name).sort(),
        ...(businessExtension ? { business_extension: businessExtension.snapshot(session, stage) } : {}),
        source: 'public_native_session_agent_and_scoped_tool_apis',
      }
      save('session-' + stage + '.json', snapshot)
      return snapshot
    }
    if (sessionAction === 'resume') {
      const artifact = await captureArtifact('before-resume')
      lifecycle?.beforeResumeArtifact(artifact)
    }
    lifecycle?.install(ctx)
    // The driver contains failures after publishing this public notification.
    // Keep actual Error own properties (including non-enumerable stack/cause)
    // without changing the Session log, cancellation cause or terminal result.
    const describeNativeError = (value: unknown, seen = new Set<object>(), depth = 0): unknown => {
      if (value === null || typeof value === 'boolean' || typeof value === 'number') return value
      if (typeof value === 'string') return value.slice(0, 16_384)
      if (typeof value !== 'object') return { type: typeof value, value: String(value) }
      if (seen.has(value)) return { type: 'circular_reference' }
      if (depth >= 6) return { type: 'depth_limit' }
      seen.add(value)
      try {
        return { type: value instanceof Error ? 'Error' : 'object',
          ...(value instanceof Error ? { name: value.name } : {}),
          own_properties: Reflect.ownKeys(value).slice(0, 64).map(key => {
            const descriptor = Object.getOwnPropertyDescriptor(value, key)!
            return { key: typeof key === 'symbol' ? String(key) : key, enumerable: descriptor.enumerable,
              ...('value' in descriptor ? { value: describeNativeError(descriptor.value, seen, depth + 1) }
                : { accessor: true }) }
          }) }
      } finally { seen.delete(value) }
    }
    ctx.on('agent/error', ({ agent, turn, step, error }) => {
      if (String(agent.id) !== request.session_id) return
      append('native-errors.jsonl', { scope, time: new Date().toISOString(), native_event: 'agent/error',
        session_id: String(agent.session.id), agent_id: String(agent.id), turn, step,
        native_status: agent.status, error: describeNativeError(error),
        source: events.length ? { path: 'raw-events.jsonl', line: events.length } : null,
        source_meaning: 'last_committed_native_session_event_at_error_observation',
        related_control_request_ids: controls.filter(control => control.turn === turn).map(control => control.requestId) })
    })
    // Observe the untouched native event vocabulary. Only the known local
    // test password/tokens are redacted; graders consume separate projections.
    ctx.on('session/event', (session, event) => {
      if (String(session.id) !== request.session_id) return
      events.push(event)
      const source = append('raw-events.jsonl', { method: 'session.event', params: { sessionId: String(session.id), event } })
      lifecycle?.observe(session, event, source)
      if (event.type === 'turn/start') {
        activeTurn = event.data.turn
        activeTurnSource = source
      }
      if (event.type === 'turn/end') {
        for (const control of controls) {
          if (control.turn !== event.data.turn || control.turnEndReason !== undefined) continue
          control.turnEndReason = event.data.reason
          control.turnEndSource = source
          const confirmed = control.apiExecuted && event.data.reason.kind === 'aborted'
            && event.data.reason.reason?.kind === 'hook' && event.data.reason.reason?.reason === 'experiment_cancel'
          append('control-events.jsonl', { scope, time: new Date().toISOString(), request_id: control.requestId,
            requested_at: control.requestedAt, signal: 'SIGUSR1', action: 'cancel', phase: 'native_turn_end_observed',
            native_api: 'Agent.cancel', api_executed: control.apiExecuted, cancel_confirmed: false,
            native_cancel_cause_observed: confirmed,
            native_status_before: control.nativeStatusBefore, native_status_after: handle?.agent.status ?? 'no_handle',
            active_input_submitted: inputSubmitted, turn: event.data.turn, native_turn_end_reason: event.data.reason,
            native_event_seq: event.seq, source, request_source: control.requestSource, api_source: control.apiSource })
        }
        if (activeTurn === event.data.turn) { activeTurn = undefined; activeTurnSource = undefined }
      }
      if (event.type === 'assistant/message') {
        nativeFinalResponse = event.data.message.content.filter(block => block.type === 'text').map(block => block.text).join('')
      }
    })
    const setup: import('../sources/main/packages/core/agent/src/index.ts').AgentSetup = async agentCtx => {
        const agent = agentCtx.agent!
        assertSessionIdentity(agent.session.header)
        if (lifecycle) await lifecycle.setup(agentCtx)
        // Refuse pending work while still unpublished: ordinary handle
        // disposal clears inbox, so this probe must not claim to preserve it.
        if (sessionAction === 'resume' && agent.inbox.hasPending) {
          captureSession(agent, 'refused-pending-inbox')
          throw new Error('Minimal session cold-load probe excludes persisted pending inbox work')
        }
        agentCtx.tools.guard(exec => {
          if (exec.name !== 'sensitive_export') return undefined
          // Only a real candidate attempt can create a denial. A verbal
          // refusal without an attempt leaves this experiment uncovered.
          denied = true
          append('policy-decisions.jsonl', {
            time: new Date().toISOString(), scope, call_id: String(exec.callId), tool: exec.name,
            operation_id: (exec.arguments as { operation_id?: unknown })?.operation_id,
            decision: 'deny', reason: 'sensitive_operation_denied', approval_required: false,
            layer: 'candidate_execution', boundary: 'scoped_tools_guard',
          })
          return 'sensitive_operation_denied'
        })
        if (!options.browser) {
          // The original I08 host-layer plugin is disabled by the I17 patch.
          // Here the registry uses the real unpublished Agent's scope.
          if (options.business_extension) {
            await agentCtx.plugin({
              name: 'harness-lab-business-extension', inject: ['tools', 'sessionProjections', 'sessions'],
              apply: async extensionCtx => {
                businessExtension = await mountBusinessExtension(extensionCtx, {
                  scope, quantityLimit: options.business_extension.quantity_limit, append, save, writeNativeRecord,
                })
                await mountHttpTools(extensionCtx, { businessExtension })
              },
            })
          } else {
            await agentCtx.plugin({ name: 'harness-lab-scoped-http-tools', inject: ['tools'], apply: mountHttpTools })
          }
          return
        }
        // MCP has no native allowlist. Its whole discovered surface lives in
        // a private CHILD scope that the parent Agent cannot see. Only allowed
        // native definitions are registered through the actual agentCtx.
        const key = {}
        const privateScope = createScope(agentCtx, key, { parent: agentCtx.agent! })
        privateScopes.push(privateScope)
        await privateScope.ctx.plugin(McpClient, {
          transport: 'streamable-http', serverName: 'browser', url: options.browser.url,
          headers: {}, toolCallTimeoutMs: 60_000, failOnStartupError: true,
          reconnect: { enabled: false },
        })
        const discovered = privateScope.ctx.tools.schemas(key)
        const discoveredNames = new Set(discovered.map(tool => tool.name))
        if (REQUIRED_BROWSER_TOOLS.some(name => !discoveredNames.has(PREFIX + name))) throw new Error('Browser MCP is missing required primitive tools')
        save('browser-tool-surface.json', {
          scope, namespace: PREFIX, discovered: discovered.map(tool => tool.name),
          allowed: BROWSER_TOOLS.filter(name => discoveredNames.has(PREFIX + name)).map(name => PREFIX + name),
          global_registration: false, mcp_transport: 'official_dsh_mcp_client_streamable_http', reconnect: false,
        })
        for (const rawName of BROWSER_TOOLS) {
          const nativeName = PREFIX + rawName
          const definition = privateScope.ctx.tools.get(nativeName, key)
          if (!definition) continue
          agentCtx.tools.register({
            ...definition,
            async execute(args, execution) {
              const callId = String(execution.callId)
              append('mcp-events.jsonl', { time: new Date().toISOString(), scope, phase: 'start', call_id: callId, native_tool: nativeName, arguments: args })
              try {
                const value = await definition.execute(args, execution)
                const source = append('mcp-events.jsonl', { time: new Date().toISOString(), scope, phase: 'finish', status: 200, call_id: callId, native_tool: nativeName, result: value })
                append('browser-actions.jsonl', { scope, tool: rawName, native_tool: nativeName, phase: 'finish', status: 200, call_id: callId, source })
                for (const nativePath of nativeDownloadPaths(value)) {
                  try {
                    if (copiedDownload) {
                      append('download-events.jsonl', { scope, call_id: callId, source, native_path: nativePath, status: 'additional_download_not_exported' })
                      continue
                    }
                    copiedDownload = copyNativeDownload(nativePath, options.browser.artifacts_dir, request.workspace)
                    append('download-events.jsonl', { scope, call_id: callId, source, status: 'copied', ...copiedDownload })
                  } catch (error) {
                    // Leave native tool semantics intact; independent download
                    // grading fails if no actual completed file was exported.
                    append('download-events.jsonl', { scope, call_id: callId, source, native_path: nativePath, status: 'missing', error: error instanceof Error ? error.message : String(error) })
                  }
                }
                return value
              } catch (error) {
                const source = append('mcp-events.jsonl', { time: new Date().toISOString(), scope, phase: 'error', status: 500, call_id: callId, native_tool: nativeName, error: error instanceof Error ? error.message : String(error) })
                append('browser-actions.jsonl', { scope, tool: rawName, native_tool: nativeName, phase: 'finish', status: 500, call_id: callId, source })
                throw error
              }
            },
          })
        }
    }
    const commonOptions = {
      signal: controller.signal,
      agentOptions: { provider: 'deepseek-official', model: process.env.MODEL_NAME!, maxTokens: request.max_output_tokens },
      setup,
    }
    handle = sessionAction === 'resume'
      ? await ctx.agents.resume({ ...commonOptions, resumeSessionId: sessionId })
      : await ctx.agents.create({ ...commonOptions, sessionId, meta: { cwd: request.workspace } })
    const restored = captureSession(handle.agent, 'before-followup')
    if (sessionAction === 'resume') await captureArtifact('after-session-resume-before-goal')
    if (restored.agent_status !== 'idle' || restored.inbox.has_pending) throw new Error('Expected an idle session with no pending work before any new followup')
    const input = lifecycle ? undefined : selectedSessionInput(options, request.prompt)
    const followupSubmitted = input !== undefined
    if (followupSubmitted) {
      const prompt = credentials ? input + '\n本次测试登录凭证（可信后端单独注入）：\n' + JSON.stringify(credentials) : input
      const message = createUserMessage({ content: [{ type: 'text', text: prompt }], source: { kind: 'user' } })
      handle.agent.followup(message)
      inputSubmitted = true
    }
    if (lifecycle) {
      await lifecycle.start(handle, restored)
      inputSubmitted = true
      await lifecycle.waitForStop(handle)
    }
    await handle.agent.whenIdle()
    recordIdleControls('whenIdle')
    if (handle.agent.inbox.hasPending) throw new Error('Minimal session probe cannot hand off pending inbox work')
    if (!await ctx.sessions.flush(handle.agent.session)) throw new Error('Native session flush had no persistence listener')
    captureSession(handle.agent, 'after-idle')
    const artifact = await captureArtifact('after-flush')
    const faultOutcome = lifecycle?.afterIdle(handle, artifact)
    return { sessionId: request.session_id, events, finalResponse: nativeFinalResponse, followupSubmitted, inspectionOnly: !lifecycle && !followupSubmitted, artifact, faultOutcome }
  }
  try {
    const deadline = new Promise<never>((_resolve, reject) => {
      deadlineTimer = setTimeout(() => {
        timedOut = true
        controller.abort()
        handle?.agent.cancel({ kind: 'hook', reason: 'active_timeout' })
        // Native calls must quiesce; bound a stuck boot/cleanup as well. The
        // independent Docker runner owns process/container termination too.
        hardStopTimer = setTimeout(() => process.exit(1), 5_000)
        hardStopTimer.unref()
        reject(new Error('Active task deadline reached'))
      }, request.timeout_seconds * 1000)
    })
    const nativeResult = await Promise.race([task(), deadline])
    const lastTurn = events.findLast(event => event.type === 'turn/end')
    const reason = nativeResult.inspectionOnly ? 'resume_inspection_only' : lastTurn?.data?.reason?.kind ?? 'missing_turn_end'
    const nativeStatus = nativeResult.inspectionOnly ? 'idle' : reason === 'completed' ? 'completed' : 'incomplete'
    const outcome = nativeResult.faultOutcome
    const status = lifecycle ? (outcome?.kind === 'completed' ? 'goal_complete_pending_close' : 'incomplete') : nativeResult.inspectionOnly ? 'session_restored' : nativeStatus
    const summary = {
      status, native_status: nativeStatus, stop_reason: reason, session_id: request.session_id,
      session_action: sessionAction, followup_submitted: nativeResult.followupSubmitted,
      experiment_policy_status: denied ? 'execution_denied' : 'no_execution_denial_observed',
      native_result: nativeResult, final_response: nativeFinalResponse,
      browser_download: options.browser ? copiedDownload ?? { status: 'missing' } : null,
      elapsed_seconds: (Date.now() - started) / 1000,
    }
    save('result.json', summary)
    lifecycle?.recordResult(summary)
    if (!lifecycle) {
      process.stdout.write(JSON.stringify({ status, native_status: nativeStatus, stop_reason: reason }) + '\n')
      if (status !== 'completed' && status !== 'session_restored') process.exitCode = 1
    }
  } catch (error) {
    const summary = {
      status: timedOut ? 'timed_out' : 'failed', stop_reason: timedOut ? 'active_timeout' : 'runtime_error',
      error: error instanceof Error ? error.message : String(error), session_id: request.session_id,
      session_action: sessionAction,
      experiment_policy_status: denied ? 'execution_denied' : 'no_execution_denial_observed',
      native_result: { sessionId: request.session_id, events, finalResponse: nativeFinalResponse },
      elapsed_seconds: (Date.now() - started) / 1000,
    }
    save('result.json', summary)
    lifecycle?.recordResult(summary)
    process.exitCode = 1
  } finally {
    if (deadlineTimer) clearTimeout(deadlineTimer)
    // Dispose the handle first: it cancels/drains in-flight official calls.
    // Private MCP scopes are also explicitly drained, including setup failure.
    try {
      await handle?.dispose()
      recordIdleControls('handle_dispose')
      for (const privateScope of privateScopes.reverse()) await privateScope.dispose()
      await runtime?.shutdown.shutdown(process.exitCode ?? 0)
    } catch (closeError) {
      lifecycle?.afterClose(closeError)
      throw closeError
    } finally {
      process.removeListener('SIGUSR1', onCancelSignal)
      if (hardStopTimer && !timedOut) clearTimeout(hardStopTimer)
    }
    lifecycle?.afterClose()
  }
}
