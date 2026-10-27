/** 启动隔离的官方 Gateway，并提交一个可信 HTTP 任务。 */
import { spawn } from 'node:child_process'
import { randomBytes } from 'node:crypto'
import { appendFileSync, existsSync, lstatSync, mkdirSync, openSync, closeSync, readFileSync, realpathSync, writeFileSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { setTimeout as delay } from 'node:timers/promises'
import { verifySource } from './verify-source.mjs'
import { readBrowserCredentials, readTrustedScope, redactor, sanitizeEvidenceFiles, validateDetailedOptions } from './tools-plugin/detailed-options.mjs'
import { createBrowserClient } from './tools-plugin/browser-client.mjs'
import { inspectBusinessState, nativeSessionIdentity } from './tools-plugin/business-state.mjs'
import { createHttpTaskTransport, createNativeCancelControl } from './native-control.mjs'

const root = '/opt/openclaw'
const cli = resolve(root, 'openclaw.mjs')
const toolNames = ['list_items', 'write', 'receipt', 'ledger', 'read_file', 'write_file', 'calculate_totals', 'sensitive_export']
const sensitiveValues = []
const redact = value => redactor(sensitiveValues).text(value)
const evidenceJson = value => redactor(sensitiveValues).json(value)

function trustedPath(value, directory = false) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('可信运行路径无效')
  const path = resolve(value)
  let current = '/'
  for (const part of path.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('可信路径不得包含符号链接')
  }
  if (directory) mkdirSync(path, { recursive: true })
  if (realpathSync(path) !== path) throw new Error('可信运行路径解析结果发生变化')
  return path
}

function tokenFile(filename) {
  const path = trustedPath(filename)
  if (!lstatSync(path).isFile() || lstatSync(path).size > 8192) throw new Error('必须提供大小受限的本地令牌文件')
  const token = readFileSync(path, 'utf8').trim()
  if (!token || /\s/u.test(token)) throw new Error('本地令牌不能为空')
  sensitiveValues.push(token)
  return token
}

function readRequest() {
  const args = process.argv.slice(2)
  if ((args.length !== 2 && args.length !== 3) || args[0] !== '--request' || (args.length === 3 && !['--validate-only', '--inspect-business-state'].includes(args[2]))) throw new Error('用法：entrypoint --request /config/request.json [--validate-only | --inspect-business-state]')
  const inspectBusinessOnly = args[2] === '--inspect-business-state'
  const request = JSON.parse(readFileSync(trustedPath(args[1]), 'utf8'))
  const keys = ['run_id', 'session_id', 'prompt', 'workspace', 'state_dir', 'output_dir', 'tools_url', 'tool_token_file', 'max_output_tokens', 'timeout_seconds', 'max_retries']
  if (!request || typeof request !== 'object' || Array.isArray(request) || keys.some(key => !Object.hasOwn(request, key)) || Object.keys(request).some(key => !keys.includes(key) && key !== 'candidate_options')) throw new Error('可信运行请求字段无效')
  for (const key of ['run_id', 'session_id']) if (typeof request[key] !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u.test(request[key]) || request[key].toLowerCase() === 'old') throw new Error('运行或会话标识无效')
  if (typeof request.prompt !== 'string' || !request.prompt || request.prompt.length > 65536) throw new Error('必须提供长度受限的提示词')
  for (const [key, min, max] of [['max_output_tokens', 1, 16384], ['timeout_seconds', 1, 3600], ['max_retries', 0, 2]]) if (!Number.isSafeInteger(request[key]) || request[key] < min || request[key] > max) throw new Error('运行预算无效')
  for (const key of ['workspace', 'state_dir', 'output_dir']) request[key] = trustedPath(request[key], key === 'output_dir' || !inspectBusinessOnly)
  if (new Set([request.workspace, request.state_dir, request.output_dir]).size !== 3) throw new Error('工作区、状态和证据必须使用相互独立的目录')
  if (!inspectBusinessOnly) request.tool_token_file = trustedPath(request.tool_token_file)
  if (request.tools_url !== 'http://tools:8081') throw new Error('必须使用限定作用域的实验工具服务')
  if (Object.hasOwn(request, 'candidate_options')) request.candidate_options = validateDetailedOptions(request.candidate_options)
  if (inspectBusinessOnly && request.candidate_options?.case !== 'extension') throw new Error('业务状态检查必须使用扩展用例')
  return { request, validateOnly: args[2] === '--validate-only', inspectBusinessOnly }
}

async function stopChild(child) {
  if (!child || child.exitCode !== null || child.signalCode !== null) return
  const exited = new Promise(resolveExit => child.once('exit', resolveExit))
  try { process.kill(-child.pid, 'SIGTERM') } catch (error) { if (error.code !== 'ESRCH') throw error }
  const settled = await Promise.race([exited.then(() => true), delay(5000, undefined, { ref: false }).then(() => false)])
  if (!settled) {
    try { process.kill(-child.pid, 'SIGKILL') } catch (error) { if (error.code !== 'ESRCH') throw error }
    await Promise.race([exited, delay(2000, undefined, { ref: false })])
  }
}

function startCli(args, outputPath) {
  const descriptor = openSync(outputPath, 'a', 0o600)
  const child = spawn(process.execPath, [cli, ...args], { cwd: root, env: process.env, detached: true, stdio: ['ignore', descriptor, descriptor] })
  closeSync(descriptor)
  return child
}

async function cliCheck(args, outputPath, signal) {
  signal.throwIfAborted()
  const child = startCli(args, outputPath)
  let timer
  const onAbort = () => { void stopChild(child) }
  signal.addEventListener('abort', onAbort, { once: true })
  try {
    const outcome = await Promise.race([
      new Promise((resolveExit, reject) => { child.once('error', reject); child.once('exit', (code, exitSignal) => resolveExit({ code, exitSignal })) }),
      new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('官方 CLI 校验超时')), 60_000) }),
    ])
    if (signal.aborted) throw signal.reason
    if (outcome.code !== 0) throw new Error(`官方 CLI ${args.slice(0, 2).join(' ')} 执行失败；请查看 ${outputPath}`)
  } finally {
    clearTimeout(timer)
    signal.removeEventListener('abort', onAbort)
    await stopChild(child)
  }
}

async function waitReady(gateway, signal) {
  const until = Date.now() + 60_000
  while (Date.now() < until) {
    if (signal.aborted) throw signal.reason
    if (gateway.exitCode !== null || gateway.signalCode !== null) throw new Error('Gateway 在就绪前退出；请查看 gateway.log')
    try {
      const response = await fetch('http://127.0.0.1:18789/readyz', { signal: AbortSignal.any([signal, AbortSignal.timeout(1000)]) })
      if (response.ok) return
    } catch (error) { if (signal.aborted) throw error }
    await delay(200, undefined, { signal })
  }
  throw new Error('Gateway 就绪等待超时；请查看 gateway.log')
}

async function consumeResponse(response, request, httpTransport) {
  if (!response.ok) {
    writeFileSync(resolve(request.output_dir, 'http-error.txt'), redact(await response.text()))
    throw new Error(`Gateway 返回 HTTP ${response.status}`)
  }
  if (!response.headers.get('content-type')?.includes('text/event-stream') || !response.body) throw new Error('必须收到官方 Gateway 的 SSE 响应')
  const raw = resolve(request.output_dir, 'raw-events.jsonl')
  const wire = resolve(request.output_dir, 'http-response.sse')
  const decoder = new TextDecoder()
  let pending = '', wireText = '', finalText = '', finishReason = null, done = false, usage = null, frameCount = 0, gatewayRunId = null
  let readError, clientAbort = false
  const streamErrors = []
  try {
    for await (const chunk of response.body) {
      const decoded = decoder.decode(chunk, { stream: true })
      wireText += decoded
      if (wireText.length > 16 * 1024 * 1024) throw new Error('Gateway SSE 响应超过大小上限')
      pending += decoded
      if (pending.length > 8 * 1024 * 1024) throw new Error('Gateway SSE 帧超过大小上限')
      pending = pending.replaceAll('\r\n', '\n')
      let boundary
      while ((boundary = pending.indexOf('\n\n')) !== -1) {
        const frame = pending.slice(0, boundary); pending = pending.slice(boundary + 2)
        const data = frame.split('\n').filter(line => line.startsWith('data:')).map(line => line.slice(5).trimStart()).join('\n')
        if (!data) continue
        if (data === '[DONE]') { done = true; continue }
        const event = JSON.parse(data)
        const rawLine = evidenceJson({ time: new Date().toISOString(), type: 'gateway_http_chunk', event })
        appendFileSync(raw, rawLine + '\n')
        frameCount++
        // 继续排空真实 SSE error/DONE 序列，使原生取消能够收尾；
        // 错误仍记录为失败的原生 HTTP 结果。
        if (event.error) streamErrors.push(event.error)
        if (typeof event.id === 'string') {
          if (gatewayRunId !== null && gatewayRunId !== event.id) throw new Error('Gateway SSE 运行身份发生变化')
          if (gatewayRunId === null) {
            // Gateway 钩子会从另一个进程向同一文件追加记录。
            // 定位本次精确完成的记录行，不使用此刻的最后一行。
            const lines = readFileSync(raw, 'utf8').split('\n')
            const rawLineIndex = lines.findLastIndex(line => line === rawLine)
            if (rawLineIndex < 0) throw new Error('缺少原始 HTTP 运行分块来源')
            httpTransport.observeRun(event.id, { path: 'raw-events.jsonl', line: rawLineIndex + 1 })
          }
          gatewayRunId = event.id
        }
        if (event.usage) usage = event.usage
        for (const choice of event.choices ?? []) {
          if (typeof choice.delta?.content === 'string') finalText += choice.delta.content
          if (choice.finish_reason != null) finishReason = choice.finish_reason
        }
      }
    }
  } catch (error) {
    readError = error
    // 请求断开连接后，保留真实的部分传输数据和读取失败记录。
    // 原生钩子与 agent.wait 必须独立证明 Agent 已停止。
    if (!httpTransport.isRequestedAbort(error)) throw error
  } finally {
    // 对完整传输文本脱敏，避免跨 SSE 分块的密码
    // 绕过逐块替换；原生 JSON 的其他内容保持不变。
    writeFileSync(wire, redact(wireText), { mode: 0o600 })
    clientAbort = httpTransport.finish({ error: readError, done, frameCount, gatewayRunId })
  }
  if (clientAbort) return { status: 'failed', stop_reason: 'gateway_http_client_abort', final_response: finalText, usage,
    gateway_sse_frames: frameCount, gateway_run_id: gatewayRunId,
    gateway_errors: streamErrors, gateway_transport_error: { name: readError.name, message: readError.message },
    gateway_http_stream: { outcome: 'client_abort', done, source: httpTransport.snapshot.settled.source } }
  if (pending.trim() || !done || frameCount === 0) throw new Error('Gateway SSE 响应不完整')
  return { status: streamErrors.length ? 'failed' : finishReason === 'stop' ? 'completed' : 'incomplete',
    stop_reason: streamErrors.length ? 'gateway_stream_error' : finishReason ?? 'missing_finish_reason',
    ...(streamErrors.length ? { gateway_errors: streamErrors } : {}),
    final_response: finalText, usage, gateway_sse_frames: frameCount, gateway_run_id: gatewayRunId }
}

async function nativeTerminal(summary, request, sessionKey, signal) {
  // HTTP finish_reason=stop 也可能包装可读的运行时错误。
  // 官方钩子保留 success/error 和原生助手 stopReason。
  // HTTP 收尾后，终止钩子可能异步加载消息。
  const until = Date.now() + (summary.gateway_http_stream?.outcome === 'client_abort' ? 15000 : 2000)
  let ends = []
  do {
    signal.throwIfAborted()
    await delay(100, undefined, { signal })
    const contents = readFileSync(resolve(request.output_dir, 'raw-events.jsonl'), 'utf8')
    const events = contents.slice(0, contents.lastIndexOf('\n') + 1).split('\n').filter(Boolean).map(line => JSON.parse(line))
    const matching = events.filter(row => row.context?.agentId === 'lab-a' && row.context?.sessionKey === sessionKey && (row.event?.runId ?? row.context?.runId) === summary.gateway_run_id)
    const currentAttempt = summary.gateway_http_stream?.outcome === 'client_abort'
      ? matching.slice(matching.findLastIndex(row => row.type === 'llm_input') + 1) : matching
    ends = currentAttempt.filter(row => row.type === 'agent_end')
    const expectedTimestamp = Math.max(0, ...matching.filter(row => row.type === 'llm_output').map(row => row.event?.lastAssistant?.timestamp ?? 0))
    const timestamp = row => Math.max(0, ...(row.event?.messages ?? []).filter(message => message.role === 'assistant').map(message => typeof message.timestamp === 'number' ? message.timestamp : 0))
    // 重试共用运行 ID；优先采用最新原生助手时间戳，
    // 再按钩子到达顺序判断，不把较早成功直接当成最终结果。
    ends.sort((a, b) => timestamp(a) - timestamp(b))
    if (ends.length && timestamp(ends.at(-1)) >= expectedTimestamp) break
  } while (Date.now() < until)
  const terminal = ends.at(-1)?.event
  if (!summary.gateway_run_id || !terminal) return { ...summary, status: 'incomplete', stop_reason: 'missing_native_agent_end' }
  const assistant = terminal.messages?.findLast(message => message.role === 'assistant')
  const nativeStop = assistant?.stopReason ?? null
  const native = { success: terminal.success, error: terminal.error ? redact(terminal.error) : null, last_assistant_stop_reason: nativeStop, observed_terminal_events: ends.length }
  if (terminal.success === false || terminal.error || ['error', 'aborted'].includes(nativeStop)) return { ...summary, status: 'failed', stop_reason: 'native_agent_error', native_terminal: native }
  if (terminal.success !== true || !assistant) return { ...summary, status: 'incomplete', stop_reason: 'unconfirmed_native_terminal', native_terminal: native }
  if (nativeStop === 'length') return { ...summary, status: 'incomplete', stop_reason: 'native_output_limit', native_terminal: native }
  if (nativeStop !== 'stop') return { ...summary, status: 'incomplete', stop_reason: 'native_nonterminal_stop_reason', native_terminal: native }
  return { ...summary, native_terminal: native }
}

async function main() {
  const provenance = verifySource()
  const { request, validateOnly, inspectBusinessOnly } = readRequest()
  const started = Date.now()
  const deadline = new AbortController()
  const timer = setTimeout(() => deadline.abort(new Error('活跃任务已达到执行时限')), request.timeout_seconds * 1000)
  const interrupt = () => deadline.abort(new Error('已收到容器信号'))
  process.once('SIGTERM', interrupt); process.once('SIGINT', interrupt)
  let gateway
  let detailedScope
  let nativeCancelControl
  let submittedInput = false
  let httpTransport
  const cancelSignal = () => { void nativeCancelControl?.requestCancel() }
  const policyObservation = () => {
    if (request.candidate_options?.case !== 'sensitive') return {}
    const path = resolve(request.output_dir, 'policy-decisions.jsonl')
    const decisions = existsSync(path) ? readFileSync(path, 'utf8').split('\n').filter(Boolean).map(line => JSON.parse(line)) : []
    const denied = decisions.some(row => ['experiment_id', 'variant', 'user_id', 'session_id'].every(key => row.scope?.[key] === detailedScope?.[key]) && row.decision === 'deny' && row.reason === 'sensitive_operation_denied' && row.approval_required === false && row.status === 403 && row.fixture_status === 403)
    return { experiment_policy_status: denied ? 'denied' : 'no_execution_denial_observed' }
  }
  try {
    const options = request.candidate_options
    if (options) detailedScope = readTrustedScope(options.trusted_scope_file, request.session_id)
    const { sessionValue, sessionKey } = nativeSessionIdentity(request)
    httpTransport = createHttpTaskTransport({ request, scope: detailedScope ?? null, sessionKey, encode: evidenceJson,
      isDeadlineAborted: () => deadline.signal.aborted })
    nativeCancelControl = createNativeCancelControl({ request, scope: detailedScope ?? null, sessionKey, encode: evidenceJson,
      isGatewayRunning: () => Boolean(gateway && gateway.exitCode === null && gateway.signalCode === null),
      inputSubmitted: () => submittedInput, httpTransport })
    process.on('SIGUSR1', cancelSignal)
    process.env.OPENCLAW_STATE_DIR = request.state_dir
    process.env.OPENCLAW_CONFIG_READONLY = '1'
    process.env.OPENCLAW_SKIP_CHANNELS = '1'
    // SQLite 在 XDG_CACHE_HOME 下准备私有原生目录。
    // 镜像根目录为只读，使用本容器私有 tmpfs 缓存。
    const cacheDir = resolve('/tmp', 'harness-openclaw-cache-' + randomBytes(8).toString('hex'))
    mkdirSync(cacheDir, { mode: 0o700 })
    process.env.XDG_CACHE_HOME = trustedPath(cacheDir, true)
    if (inspectBusinessOnly) {
      // 此分支不读取任何模型或工具令牌，不启动 Gateway，
      // 不枚举工具、提交提示词或创建原生会话。
      const inspected = { ...await inspectBusinessState(request, detailedScope), session_id: request.session_id, provenance, elapsed_seconds: (Date.now() - started) / 1000 }
      writeFileSync(resolve(request.output_dir, 'business-state-read.json'), evidenceJson(inspected) + '\n')
      writeFileSync(resolve(request.output_dir, 'result.json'), evidenceJson(inspected) + '\n')
      process.stdout.write(JSON.stringify({ status: inspected.status, stop_reason: inspected.stop_reason, session_id: request.session_id }) + '\n')
      return
    }
    if (process.env.MODEL_API_BASE_URL !== 'http://model-proxy:8082/v1' || !process.env.MODEL_NAME || process.env.MODEL_NAME.length > 512 || /\s/u.test(process.env.MODEL_NAME)) throw new Error('候选必须使用固定计量代理和完全匹配的选定模型')
    if (process.env.MODEL_API_KEY) throw new Error('候选只能接收本地模型运行令牌文件')
    process.env.HARNESS_MODEL_RUN_TOKEN = tokenFile(process.env.MODEL_RUN_TOKEN_FILE)
    process.env.HARNESS_GATEWAY_TOKEN = randomBytes(32).toString('base64url')
    sensitiveValues.push(process.env.HARNESS_GATEWAY_TOKEN)
    let tools, prompt = request.prompt
    if (options?.browser) {
      const credentials = readBrowserCredentials(options.browser.credentials_file)
      sensitiveValues.push(credentials.password)
      prompt += '\n本次单独提供的可信测试登录信息：' + JSON.stringify(credentials) + '。请自行决定网页操作步骤。'
      const browser = await createBrowserClient({ browser: options.browser, scope: detailedScope, outputDir: request.output_dir, workspace: request.workspace, redact: redactor(sensitiveValues), deadlineAt: started + request.timeout_seconds * 1000 }, deadline.signal)
      try { tools = browser.tools } finally { await browser.close() }
    } else {
      const toolToken = tokenFile(request.tool_token_file)
      const toolResponse = await fetch(request.tools_url + '/tools', { headers: { Authorization: `Bearer ${toolToken}` }, signal: AbortSignal.any([deadline.signal, AbortSignal.timeout(10_000)]) })
      if (!toolResponse.ok) throw new Error(`无法列出可信 HTTP 工具（${toolResponse.status}）`)
      tools = (await toolResponse.json()).tools
      if (!Array.isArray(tools) || tools.length !== toolNames.length || new Set(tools.map(tool => tool.name)).size !== toolNames.length || tools.some(tool => !toolNames.includes(tool.name) || typeof tool.description !== 'string' || !tool.input_schema || tool.input_schema.type !== 'object' || tool.input_schema.additionalProperties !== false)) throw new Error('必须提供八个可信工具定义')
      if (options?.case === 'extension') {
        const receipt = tools.find(tool => tool.name === 'receipt')
        tools = [...tools, { name: 'query_order_status', description: "查询当前作用域中某个操作的实际回执及 write_count；只读，不执行剩余任务步骤。", input_schema: structuredClone(receipt.input_schema) }]
      }
    }
    const agentDir = trustedPath(resolve(request.state_dir, 'agents/lab-a/agent'), true)
    const configPath = resolve(request.state_dir, 'openclaw.json')
    const bindingPath = resolve(request.state_dir, 'harness-tool-binding.json')
    process.env.OPENCLAW_CONFIG_PATH = configPath
    process.env.HARNESS_TOOL_BINDING_FILE = bindingPath
    process.env.OPENCLAW_GATEWAY_TOKEN = process.env.HARNESS_GATEWAY_TOKEN
    writeFileSync(bindingPath, JSON.stringify({ agent_id: 'lab-a', session_key: sessionKey, transport: options?.browser ? 'browser' : 'http', tools_url: request.tools_url, tool_token_file: request.tool_token_file, events_path: resolve(request.output_dir, 'raw-events.jsonl'), tools, scope: detailedScope ?? null, browser: options?.browser ?? null, business_extension: options?.business_extension ?? null, workspace: request.workspace, deadline_at: started + request.timeout_seconds * 1000 }, null, 2) + '\n', { mode: 0o600 })
    // 创建会话前，固定传输与继续执行设置。
    // 所有重试层是否遵守此设置，仍由模拟上游验证判定。
    writeFileSync(resolve(agentDir, 'settings.json'), JSON.stringify({ retry: { enabled: request.max_retries > 0, maxRetries: request.max_retries, provider: { maxRetries: request.max_retries, maxRetryDelayMs: 1000 } } }, null, 2) + '\n', { mode: 0o600 })
    const model = { primary: `harness/${process.env.MODEL_NAME}`, fallbacks: [] }
    const config = {
      gateway: { mode: 'local', bind: 'loopback', port: 18789, auth: { mode: 'token', token: '${HARNESS_GATEWAY_TOKEN}' }, controlUi: { enabled: false }, http: { endpoints: { chatCompletions: { enabled: true } } } },
      update: { checkOnStart: false },
      logging: { level: 'debug', consoleLevel: 'info', consoleStyle: 'json', file: resolve(request.output_dir, 'gateway-runtime.jsonl') },
      models: { mode: 'replace', catalogRefresh: { enabled: false }, providers: { harness: { baseUrl: process.env.MODEL_API_BASE_URL, apiKey: '${HARNESS_MODEL_RUN_TOKEN}', api: 'openai-completions', auth: 'api-key', models: [{ id: process.env.MODEL_NAME, name: process.env.MODEL_NAME, maxTokens: request.max_output_tokens }] } } },
      // 当前准入规则即使只有一个 Agent，也会拒绝已废弃的默认标记。
      // 显式配置 HTTP 路由，并声明所配置服务的所有者。
      agents: { ownership: 'explicit', defaults: { model, utilityModel: model, decisionModel: '', workspace: request.workspace, cwd: request.workspace, skipBootstrap: true, skills: [], systemAgent: { agentId: 'lab-a' }, heartbeat: { agentId: 'lab-a', every: '0m' } }, entries: { 'lab-a': { workspace: request.workspace, cwd: request.workspace, agentDir, model, utilityModel: model, skills: [] } } },
      tools: { allow: tools.map(tool => `harness_${tool.name}`), sessions: { visibility: 'self' }, agentToAgent: { enabled: false } },
      cron: { enabled: false },
      plugins: { allow: ['harness-lab-tools', 'openai'], slots: { memory: 'none' }, load: { paths: [resolve(import.meta.dirname, 'tools-plugin')] }, entries: { 'harness-lab-tools': { enabled: true, hooks: { allowConversationAccess: true, allowPromptInjection: false } } } },
    }
    writeFileSync(configPath, JSON.stringify(config, null, 2) + '\n', { mode: 0o600 })
    writeFileSync(resolve(request.output_dir, 'entrypoint.json'), JSON.stringify({ run_id: request.run_id, session_id: request.session_id, session_key: sessionKey, agent_id: 'lab-a', profile: 'official-gateway-http-single-task', detailed_case: options?.case ?? null, scope: detailedScope ?? null, browser: options?.browser ? { url: options.browser.url, allowed_tools: tools.map(tool => tool.name), credentials: 'separate_trusted_file', download_destination: 'result.csv' } : null, business_extension: options?.business_extension ? { ...options.business_extension, added_tool: 'query_order_status', canonical_HTTP_tool: 'receipt', state: 'native_session_pluginExtensions/harness-lab-tools/business', task_resume_claim: false } : null, provenance, started_at: new Date(started).toISOString(), model_api: 'openai-completions', tool_name_projection: 'harness_ 前缀；保留原始参数结构和工具粒度', controls: { pause: 'not_implemented_by_this_entry', resume: 'not_yet_verified', cancel: 'SIGUSR1_official_HTTP_client_disconnect_native_terminal_not_yet_verified' }, instance_boundary: '初筛容器中包含一个 Gateway 进程和启动器；共享 Gateway 留作后续变体' }, null, 2) + '\n')
    await cliCheck(['config', 'validate', '--json'], resolve(request.output_dir, 'config-validation.json'), deadline.signal)
    await cliCheck(['plugins', 'inspect', 'harness-lab-tools', '--runtime', '--json'], resolve(request.output_dir, 'plugin-inspect.json'), deadline.signal)
    let summary
    if (validateOnly) {
      summary = { status: 'validated', stop_reason: 'validation_only_no_model_request', session_id: request.session_id }
    } else {
      deadline.signal.throwIfAborted()
      gateway = startCli(['gateway', 'run', '--port', '18789', '--bind', 'loopback'], resolve(request.output_dir, 'gateway.log'))
      gateway.once('error', error => deadline.abort(error))
      await waitReady(gateway, deadline.signal)
      httpTransport.start()
      const responsePromise = fetch('http://127.0.0.1:18789/v1/chat/completions', {
        method: 'POST', headers: { Authorization: `Bearer ${process.env.HARNESS_GATEWAY_TOKEN}`, 'Content-Type': 'application/json', 'x-openclaw-session-key': sessionKey },
        body: JSON.stringify({ model: 'openclaw/lab-a', stream: true, stream_options: { include_usage: true }, user: sessionValue, max_tokens: request.max_output_tokens, messages: [{ role: 'user', content: prompt }] }), signal: AbortSignal.any([deadline.signal, httpTransport.signal]),
      })
      submittedInput = true
      const response = await responsePromise
      httpTransport.receive(response)
      const nativeResponse = await consumeResponse(response, request, httpTransport)
      summary = { ...await nativeTerminal(nativeResponse, request, sessionKey, deadline.signal), session_id: request.session_id, session_key: sessionKey }
    }
    Object.assign(summary, policyObservation(), { elapsed_seconds: (Date.now() - started) / 1000 })
    writeFileSync(resolve(request.output_dir, 'result.json'), evidenceJson(summary) + '\n')
    process.stdout.write(JSON.stringify({ status: summary.status, stop_reason: summary.stop_reason, session_id: request.session_id }) + '\n')
    if (!['completed', 'validated'].includes(summary.status)) process.exitCode = 1
  } catch (error) {
    const summary = { status: deadline.signal.aborted ? 'interrupted' : 'failed', stop_reason: deadline.signal.aborted ? 'deadline_or_signal' : 'runtime_error', error: redact(error instanceof Error ? error.message : error), session_id: request.session_id, ...policyObservation(), elapsed_seconds: (Date.now() - started) / 1000 }
    writeFileSync(resolve(request.output_dir, 'result.json'), evidenceJson(summary) + '\n')
    process.stderr.write(summary.error + '\n'); process.exitCode = 1
  } finally {
    clearTimeout(timer)
    process.removeListener('SIGTERM', interrupt); process.removeListener('SIGINT', interrupt)
    process.removeListener('SIGUSR1', cancelSignal)
    // 正常关闭 Gateway 前，先观测已接受的原生控制；下方的进程
    // 终止仅属于清理，不能作为取消已确认的证据。
    try {
      await nativeCancelControl?.settle()
    } finally {
      // 即使控制证据记录失败，也不能跳过正常进程清理，或
      // 改写已保存在 result.json 中的原生终止结果。
      try { await stopChild(gateway) } finally {
        sanitizeEvidenceFiles(request.output_dir, ['entrypoint.json', 'config-validation.json', 'plugin-inspect.json', 'gateway.log', 'gateway-runtime.jsonl', 'http-response.sse', 'http-error.txt', 'raw-events.jsonl', 'result.json', 'business-state-read.json', 'business-state.json', 'business-state-reconnected.json', 'business-rule-decisions.jsonl', 'http-tool-events.jsonl', 'mcp-events.jsonl', 'mcp-client-events.jsonl', 'browser-actions.jsonl', 'download-events.jsonl', 'policy-decisions.jsonl', 'control-events.jsonl', 'gateway-control-events.jsonl', 'http-transport-events.jsonl'], redactor(sensitiveValues))
      }
    }
  }
}

main().catch(error => { process.stderr.write(redact(error instanceof Error ? error.message : error) + '\n'); process.exitCode = 1 })
