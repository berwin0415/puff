/** 仅供实验控制：使用官方 HTTP 断开机制和公开终止状态查询。 */
import { spawn } from 'node:child_process'
import { createHash } from 'node:crypto'
import { appendFileSync, existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'

function rows(filename) {
  if (!existsSync(filename)) return []
  const text = readFileSync(filename, 'utf8')
  if (text.length > 32 * 1024 * 1024) throw new Error('原生事件证据超过大小上限')
  return text.slice(0, text.lastIndexOf('\n') + 1).split('\n').flatMap((line, index) => line ? [{ row: JSON.parse(line), source: { path: 'raw-events.jsonl', line: index + 1 } }] : [])
}

function hookRun(row) { return row.event?.runId ?? row.context?.runId }
function assistantStop(row) { return row.event?.messages?.findLast(message => message.role === 'assistant')?.stopReason ?? null }
function assistantTimestamp(row) {
  return Math.max(0, ...(row.event?.messages ?? []).filter(message => message.role === 'assistant').map(message => Number.isFinite(message.timestamp) ? message.timestamp : 0))
}

/** agent.wait 超时仅是观测结果，不代表已确认终止。 */
export function isCancelledTerminal(value, runId) {
  return value?.runId === runId && value.status === 'error' && value.stopReason === 'aborted'
    && Number.isFinite(value.endedAt) && value.pendingError !== true && value.yielded !== true
}

/** 仅管理此 HTTP 客户端的信号；中止只是请求，不代表已确认取消。 */
export function createHttpTaskTransport({ request, scope, sessionKey, encode, isDeadlineAborted }) {
  const controller = new AbortController()
  const transportId = request.run_id + ':http:1'
  let state = 'unsubmitted', runId, disconnect, settled, requestSource, responseSource, runSource
  const append = value => {
    const path = 'http-transport-events.jsonl', filename = resolve(request.output_dir, path)
    const line = existsSync(filename) ? readFileSync(filename, 'utf8').split('\n').length : 1
    appendFileSync(filename, encode({ scope, session_key: sessionKey, transport_id: transportId, time: new Date().toISOString(), ...value }) + '\n', { mode: 0o600 })
    return { path, line }
  }
  return {
    signal: controller.signal,
    get snapshot() { return { state, runId, transportId, settled } },
    start() {
      if (state !== 'unsubmitted') throw new Error('HTTP 任务已经提交')
      state = 'submitted'
      requestSource = append({ phase: 'requested', method: 'POST', path: '/v1/chat/completions' })
    },
    receive(response) {
      if (state !== 'submitted') throw new Error('HTTP 响应状态不符合预期')
      state = 'reading'
      responseSource = append({ phase: 'response_received', status: response.status, content_type: response.headers.get('content-type'), request_source: requestSource })
    },
    observeRun(value, rawSource) {
      if (runId !== undefined && runId !== value) throw new Error('HTTP 任务运行身份发生变化')
      if (runId === undefined) {
        runId = value
        runSource = append({ phase: 'run_observed', run_id: runId, source: rawSource, response_source: responseSource })
      }
    },
    disconnect({ requestId, nativeRunId }) {
      if (state !== 'reading' || disconnect || controller.signal.aborted || isDeadlineAborted() || runId !== nativeRunId) throw new Error('没有可断开的匹配活跃 HTTP 任务')
      const params = { method: 'POST', path: '/v1/chat/completions', transport_id: transportId, runId }
      const source = append({ phase: 'disconnect_requested', request_id: requestId, run_id: runId, params,
        run_source: runSource, signal_was_aborted: false })
      disconnect = { requestId, runId, source }
      // 官方 Gateway 监听 HTTP 连接提前关闭，并将产生的
      // abortSignal 传入未经修改的原生 Agent 命令。
      controller.abort(new DOMException('Harness 已请求断开当前 HTTP 客户端', 'AbortError'))
      const result = { transport_id: transportId, runId, disconnect_requested: true, signal_aborted: controller.signal.aborted }
      const returnedSource = append({ phase: 'disconnect_returned', request_id: requestId, run_id: runId,
        request_source: source, result, terminal_confirmation: false })
      return { result, source: returnedSource, params }
    },
    isRequestedAbort(error) {
      return Boolean(disconnect && controller.signal.aborted && !isDeadlineAborted() && error?.name === 'AbortError')
    },
    finish({ error, done, frameCount, gatewayRunId }) {
      const clientAbort = this.isRequestedAbort(error)
      state = 'settled'
      const wire = readFileSync(resolve(request.output_dir, 'http-response.sse'))
      const event = { phase: 'stream_settled', outcome: clientAbort ? 'client_abort' : error ? 'read_error' : 'complete',
        run_id: gatewayRunId, request_id: disconnect?.requestId ?? null, disconnect_source: disconnect?.source ?? null,
        run_source: runSource ?? null, request_source: requestSource, body_read_error: error ? { name: error.name, message: error.message } : null,
        done, gateway_sse_frames: frameCount, wire_sha256: createHash('sha256').update(wire).digest('hex'), terminal_confirmation: false }
      settled = { result: event, source: append(event) }
      return clientAbort
    },
  }
}

export function createNativeCancelControl({ request, scope, sessionKey, encode, isGatewayRunning, inputSubmitted, httpTransport }) {
  let sequence = 0, pending, control, finalRecorded = false
  const append = (name, value) => {
    const filename = resolve(request.output_dir, name)
    const line = existsSync(filename) ? readFileSync(filename, 'utf8').split('\n').length : 1
    appendFileSync(filename, encode(value) + '\n', { mode: 0o600 })
    return { path: name, line }
  }
  const nativeHooks = () => rows(resolve(request.output_dir, 'raw-events.jsonl')).filter(({ row }) => row.context?.agentId === 'lab-a' && row.context?.sessionKey === sessionKey)
  const rpc = async (method, params) => {
    const source = append('gateway-control-events.jsonl', { scope, time: new Date().toISOString(), phase: 'requested', method, params })
    // 官方基于配置的本地 CLI 认证从已有环境或配置
    // 读取所有者令牌；令牌不会进入 argv 或证据。
    const child = spawn(process.execPath, ['/opt/openclaw/openclaw.mjs', 'gateway', 'call', method, '--port', '18789', '--params', JSON.stringify(params), '--json', '--timeout', '20000'], {
      cwd: '/opt/openclaw', env: process.env, stdio: ['ignore', 'pipe', 'pipe'],
    })
    let stdout = '', stderr = '', timer, killTimer, settleTimer
    let boundedError, outputTruncated = false
    // 这些时限仅终止当前官方 CLI 客户端；不会杀死 Gateway
    // 来实现或确认原生取消。
    const stopHelper = error => {
      boundedError ??= error
      if (child.exitCode !== null || child.signalCode !== null || killTimer) return
      child.kill('SIGTERM')
      killTimer = setTimeout(() => child.kill('SIGKILL'), 1000)
    }
    const collect = (key, chunk) => {
      const remaining = Math.max(0, 2 * 1024 * 1024 - stdout.length - stderr.length)
      const text = chunk.toString('utf8')
      if (key === 'stdout') stdout += text.slice(0, remaining)
      else stderr += text.slice(0, remaining)
      if (text.length > remaining) {
        outputTruncated = true
        stopHelper(new Error('官方 Gateway CLI 控制响应超过大小上限'))
      }
    }
    child.stdout.on('data', chunk => collect('stdout', chunk))
    child.stderr.on('data', chunk => collect('stderr', chunk))
    let outcome
    try {
      outcome = await new Promise(resolveExit => {
        child.once('error', error => resolveExit({ code: null, signal: null, error: error.message }))
        child.once('close', (code, signal) => resolveExit({ code, signal }))
        timer = setTimeout(() => stopHelper(new Error('官方 Gateway CLI 控制已达到执行时限')), 25000)
        settleTimer = setTimeout(() => {
          stopHelper(new Error('官方 Gateway CLI 客户端清理尚未完成'))
          resolveExit({ code: null, signal: null, error: '辅助清理在限定时间内完成后，仍未确认 CLI 客户端已关闭' })
        }, 27000)
      })
      let result
      try { result = JSON.parse(stdout) } catch { /* 下方保留实际格式错误的 CLI 输出。 */ }
      const responseSource = append('gateway-control-events.jsonl', { scope, time: new Date().toISOString(), phase: 'returned', method, params,
        request_source: source, exit_code: outcome.code, exit_signal: outcome.signal,
        result: result ?? null, stdout, stderr, error: boundedError?.message ?? outcome.error ?? null, output_truncated: outputTruncated,
        evidence_kind: 'official_cli_stdout_json_not_websocket_wire' })
      if (boundedError) throw boundedError
      if (outcome.code !== 0 || !result || typeof result !== 'object') throw new Error('官方 Gateway CLI 控制失败；请查看 gateway-control-events.jsonl')
      return { result, source: responseSource }
    } finally { clearTimeout(timer); clearTimeout(killTimer); clearTimeout(settleTimer) }
  }
  const requestCancel = async () => {
    const requestedAt = new Date().toISOString()
    const requestId = request.run_id + ':cancel:' + (++sequence)
    const base = { scope, request_id: requestId, requested_at: requestedAt, signal: 'SIGUSR1', action: 'cancel',
      native_api: 'Gateway.HTTP.client_disconnect', session_key: sessionKey, active_input_submitted: inputSubmitted() }
    try {
      const hooks = nativeHooks()
      const start = hooks.findLast(({ row }) => row.type === 'llm_input' && typeof hookRun(row) === 'string')
      const runId = start && hookRun(start.row)
      const end = start && hooks.findLast(({ row, source }) => row.type === 'agent_end' && hookRun(row) === runId && source.line > start.source.line)
      const inactiveReason = pending ? 'control_already_pending' : !scope ? 'no_trusted_detailed_scope' : !isGatewayRunning() ? 'no_running_gateway'
        : !base.active_input_submitted ? 'no_submitted_input' : !start ? 'no_native_llm_input' : end ? 'native_agent_end_already_observed'
          : httpTransport.snapshot.state !== 'reading' || httpTransport.snapshot.runId !== runId ? 'no_matching_live_http_stream' : null
      if (inactiveReason) {
        append('control-events.jsonl', { ...base, time: requestedAt, phase: 'not_cancelled', reason: inactiveReason,
          api_executed: false, cancel_confirmed: false, native_status_before: null, native_status_after: null })
        return
      }
      // 立即占用控制机会：后续信号不会排队触发未来中止。
      control = { base: { ...base, native_run_id: runId, source: start.source,
        observed_activity: 'native_llm_input_without_later_agent_end', native_status_api: 'agent.wait' }, apiExecuted: false }
      const current = control
      pending = (async () => {
        const before = await rpc('agent.wait', { runId, timeoutMs: 0 })
        current.before = before
        if (before.result.runId !== runId || !['timeout', 'pending'].includes(before.result.status) || before.result.endedAt !== undefined || before.result.stopReason !== undefined) {
          append('control-events.jsonl', { ...current.base, time: new Date().toISOString(), phase: 'not_cancelled', reason: 'native_run_already_terminal_or_unconfirmed',
            api_executed: false, cancel_confirmed: false, native_status_before: before.result.status ?? null,
            native_status_after: before.result.status ?? null, native_before_result: before.result, native_before_source: before.source })
          return
        }
        // CLI 查询会让出执行；操作此 HTTP 客户端前重新检查实际钩子，
        // 避免把自然结束的运行重新标为已取消。
        if (nativeHooks().some(({ row, source }) => row.type === 'agent_end' && hookRun(row) === runId && source.line > start.source.line)) {
          append('control-events.jsonl', { ...current.base, time: new Date().toISOString(), phase: 'not_cancelled',
            reason: 'native_agent_end_during_status_query', api_executed: false, cancel_confirmed: false })
          return
        }
        const params = { method: 'POST', path: '/v1/chat/completions', transport_id: httpTransport.snapshot.transportId, runId }
        current.requestSource = append('control-events.jsonl', { ...current.base, time: new Date().toISOString(), phase: 'requested',
          api_executed: false, cancel_confirmed: false, native_status_before: before.result.status, native_status_after: before.result.status,
          native_before_result: before.result, native_before_source: before.source,
          native_params: params })
        current.apiAttempted = true
        const aborted = httpTransport.disconnect({ requestId, nativeRunId: runId })
        current.apiExecuted = true
        current.aborted = aborted
        current.apiSource = append('control-events.jsonl', { ...current.base, time: new Date().toISOString(), phase: 'native_api_returned',
          api_executed: true, cancel_confirmed: false, native_status_before: before.result.status, native_status_after: null,
          native_api_result: aborted.result, native_api_source: aborted.source, request_source: current.requestSource,
          native_status_after_meaning: 'client_disconnect_requested_not_native_terminal' })
        current.after = await rpc('agent.wait', { runId, timeoutMs: 15000 })
      })().catch(error => {
        append('control-events.jsonl', { ...current.base, time: new Date().toISOString(), phase: 'control_error',
          api_executed: current.apiExecuted ? true : current.apiAttempted ? null : false, native_api_attempted: current.apiAttempted === true, cancel_confirmed: false,
          native_status_before: current.before?.result.status ?? null, native_status_after: current.after?.result.status ?? null,
          request_source: current.requestSource, api_source: current.apiSource, error: error instanceof Error ? error.message : String(error) })
      })
      await pending
    } catch (error) {
      const failure = { ...base, time: new Date().toISOString(), phase: 'control_error', api_executed: false, cancel_confirmed: false,
        error: error instanceof Error ? error.message : String(error) }
      try { append('control-events.jsonl', failure) } catch { process.stderr.write(encode(failure) + '\n') }
    }
  }
  return {
    requestCancel,
    async settle() {
      await pending
      if (!control?.apiExecuted || finalRecorded) return
      finalRecorded = true
      const runId = control.base.native_run_id
      const hooks = nativeHooks().filter(({ row }) => hookRun(row) === runId)
      const latestInputLine = Math.max(control.base.source.line, ...hooks.filter(({ row }) => row.type === 'llm_input').map(({ source }) => source.line))
      const terminal = hooks.filter(({ row, source }) => row.type === 'agent_end' && source.line > latestInputLine)
        .sort((a, b) => assistantTimestamp(a.row) - assistantTimestamp(b.row) || a.source.line - b.source.line).at(-1)
      const stop = terminal ? assistantStop(terminal.row) : null
      if (terminal) append('control-events.jsonl', { ...control.base, time: new Date().toISOString(), phase: 'native_turn_end_observed',
        native_event: 'agent_end', api_executed: true, cancel_confirmed: false,
        native_status_before: control.before.result.status, native_status_after: control.after?.result.status ?? null,
        native_terminal: terminal.row.event, native_assistant_stop_reason: stop, source: terminal.source,
        request_source: control.requestSource, api_source: control.apiSource })
      const stream = httpTransport.snapshot.settled
      const confirmed = control.aborted.result.disconnect_requested === true && stop === 'aborted' && terminal?.row.event.success === false
        && isCancelledTerminal(control.after?.result, runId) && stream?.result.outcome === 'client_abort'
        && stream.result.request_id === control.base.request_id && stream.result.run_id === runId
      append('control-events.jsonl', { ...control.base, time: new Date().toISOString(), phase: 'native_idle_observed',
        stage: 'public_agent_wait_terminal_and_http_client_disconnected', native_state_kind: 'run_terminal_not_Agent_idle',
        api_executed: true, cancel_confirmed: confirmed, native_status_before: control.before.result.status,
        native_status_after: control.after?.result.status ?? null, native_after_result: control.after?.result ?? null,
        native_after_source: control.after?.source ?? null, native_assistant_stop_reason: stop,
        native_inbox_pending: null, http_stream_settled: stream?.result.outcome === 'client_abort', http_stream_source: { path: 'http-response.sse' },
        http_transport_source: stream?.source ?? null,
        source: terminal?.source ?? null, request_source: control.requestSource, api_source: control.apiSource,
        confirmation_reason: confirmed ? 'http_disconnect_native_aborted_agent_end_terminal_wait_and_client_stream_settled' : 'native_cancel_terminal_unconfirmed' })
    },
  }
}
