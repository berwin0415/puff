/** 为官方 Pi Agent Core 循环和 OpenAI 聊天适配器提供轻量运行入口。 */
import { appendFileSync, existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { verifySource } from './verify-source.mjs'
import { Evidence, aggregateBusinessState, readCredentials, readScope, validateOptions } from './detailed_support.mjs'

const secrets = []
const redact = value => secrets.reduce((text, token) => text.replaceAll(token, '[redacted-local-token]'), String(value))
const encode = (value, pretty = false) => redact(JSON.stringify(value, (_key, item) => item instanceof Set ? [...item] : item, pretty ? 2 : undefined)) + '\n'

function trustedPath(value, directory = false) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('可信运行路径无效')
  const path = resolve(value)
  let current = '/'
  for (const part of path.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('可信路径不能包含符号链接')
  }
  if (directory) mkdirSync(path, { recursive: true })
  if (realpathSync(path) !== path) throw new Error('可信运行路径的解析结果发生变化')
  return path
}

function tokenFile(filename) {
  const path = trustedPath(filename)
  if (!lstatSync(path).isFile() || lstatSync(path).size > 8192) throw new Error('本地令牌文件必须为普通文件，且大小不能超过限制')
  const token = readFileSync(path, 'utf8').trim()
  if (!token || /\s/u.test(token)) throw new Error('本地令牌不能为空或包含空白字符')
  secrets.push(token)
  return token
}

function readRequest() {
  if (process.argv.length !== 4 || process.argv[2] !== '--request') throw new Error('用法：entrypoint --request /config/request.json')
  const request = JSON.parse(readFileSync(trustedPath(process.argv[3]), 'utf8'))
  const keys = ['run_id', 'session_id', 'prompt', 'workspace', 'state_dir', 'output_dir', 'tools_url', 'tool_token_file', 'max_output_tokens', 'timeout_seconds', 'max_retries']
  const allowed = [...keys, 'candidate_options']
  if (!request || typeof request !== 'object' || Array.isArray(request) || Object.keys(request).some(key => !allowed.includes(key)) || keys.some(key => !Object.hasOwn(request, key))) throw new Error('可信运行请求的字段无效')
  for (const key of ['run_id', 'session_id']) if (typeof request[key] !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u.test(request[key]) || request[key].toLowerCase() === 'old') throw new Error('运行或会话标识无效')
  if (typeof request.prompt !== 'string' || !request.prompt || request.prompt.length > 65536) throw new Error('任务提示词不能为空，且长度不能超过限制')
  for (const [key, minimum, maximum] of [['max_output_tokens', 1, 16384], ['timeout_seconds', 1, 3600], ['max_retries', 0, 2]]) if (!Number.isSafeInteger(request[key]) || request[key] < minimum || request[key] > maximum) throw new Error('运行预算无效')
  for (const key of ['workspace', 'state_dir', 'output_dir']) request[key] = trustedPath(request[key], true)
  const paths = [request.workspace, request.state_dir, request.output_dir]
  if (paths.some((a, index) => paths.slice(index + 1).some(b => a === b || a.startsWith(b + '/') || b.startsWith(a + '/')))) throw new Error('工作区、状态和证据必须分别使用独立的目录树')
  if (!(request.candidate_options && request.candidate_options.case === 'browser')) request.tool_token_file = trustedPath(request.tool_token_file)
  if (request.tools_url !== 'http://tools:8081') throw new Error('必须使用限定作用域的实验工具服务')
  if (request.candidate_options) validateOptions(request.candidate_options)
  return request
}

async function loadTools(request, token, deadlineSignal) {
  const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
  const response = await fetch(new URL('/tools', request.tools_url), { headers, signal: AbortSignal.any([deadlineSignal, AbortSignal.timeout(10_000)]) })
  if (!response.ok) throw new Error(`无法列出可信 HTTP 工具 (${response.status})`)
  const payload = await response.json()
  if (!payload || !Array.isArray(payload.tools) || payload.tools.length === 0) throw new Error('可信工具定义为空')
  const seen = new Set()
  return payload.tools.map(tool => {
    if (!tool || typeof tool.name !== 'string' || !/^[A-Za-z][A-Za-z0-9_]*$/u.test(tool.name) || seen.has(tool.name) || typeof tool.description !== 'string' || !tool.input_schema || tool.input_schema.type !== 'object') throw new Error('可信工具定义无效或重复')
    seen.add(tool.name)
    return {
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.input_schema,
      async execute(_toolCallId, arguments_, signal) {
        // 不自动重试 HTTP 工具；写入已提交时仍可能丢失回执。
        const response = await fetch(new URL('/call', request.tools_url), {
          method: 'POST', headers,
          body: JSON.stringify({ name: tool.name, arguments: arguments_ }),
          signal: signal ? AbortSignal.any([deadlineSignal, signal]) : deadlineSignal,
        })
        const body = await response.json()
        return { content: [{ type: 'text', text: JSON.stringify(body) }], details: body, isError: !response.ok }
      },
    }
  })
}

function saveState(request, agent, extension = null) {
  // 这是应用自行记录的状态观察，不能作为持久化的原生检查点。
  const value = { persistence: 'application_written_observation_only', session_id: request.session_id, ...agent.state }
  if (extension) value.harness_lab_business_state = extension.state
  const path = resolve(request.state_dir, 'observed-state.json')
  writeFileSync(path + '.tmp', encode(value, true))
  renameSync(path + '.tmp', path)
  writeFileSync(resolve(request.output_dir, 'state-snapshot.json'), encode(value, true))
}

async function main() {
  const provenance = verifySource()
  const request = readRequest()
  if (process.env.MODEL_API_BASE_URL !== 'http://model-proxy:8082/v1' || !process.env.MODEL_NAME) throw new Error('候选必须使用计数代理和指定模型')
  const modelToken = process.env.MODEL_API_KEY || tokenFile(process.env.MODEL_RUN_TOKEN_FILE)
  if (process.env.MODEL_API_KEY) secrets.push(modelToken)
  const toolToken = request.candidate_options?.case === 'browser' ? null : tokenFile(request.tool_token_file)
  const { Agent, streamSimple } = await import('/opt/pi-build/packages/ai/pi-core.bundle.mjs')
  const started = Date.now()
  const deadline = new AbortController()
  let timedOut = false, agent, nativeEnd = null, transportAttempts = 0, extensionDone = null
  // 在 try 外声明，供 finally 清理引用（避免块级作用域导致的未定义）。
  let evidence = null, scope = null, browserHandle = null, observations = []
  const raw = resolve(request.output_dir, 'raw-events.jsonl')
  writeFileSync(raw, '')
  const transportLog = resolve(request.output_dir, 'transport-events.jsonl')
  writeFileSync(transportLog, '')
  const interrupt = () => { deadline.abort(new Error('收到取消进程的请求')); agent?.abort() }
  process.on('SIGTERM', interrupt); process.on('SIGINT', interrupt)
  const timer = setTimeout(() => { timedOut = true; deadline.abort(new Error('已达到任务执行时限')); agent?.abort() }, request.timeout_seconds * 1000)
  let summary
  try {
    if (existsSync(resolve(request.state_dir, 'observed-state.json'))) throw new Error('此初筛入口仅启动新会话，记录的消息尚不能用于恢复会话')
    const options = request.candidate_options
    let tools
    if (options) {
      scope = readScope(options.trusted_scope_file, request.session_id)
      evidence = new Evidence(request.output_dir, value => encode(value), scope)
    }
    if (options?.case === 'browser') {
      const credentials = readCredentials(options.browser.credentials_file, scope.user_id)
      secrets.push(credentials.password)
      request.prompt = request.prompt + `\n\n本轮测试登录资料：站点 ${credentials.url}，用户名 ${credentials.username}，密码 ${credentials.password}。`
      const mcp = await import('/opt/pi-build/packages/ai/pi-core.bundle.mjs')
      const { openBrowserTools } = await import('./browser_tools.mjs')
      browserHandle = await openBrowserTools(options.browser, request.workspace, evidence, request.timeout_seconds, mcp)
      tools = browserHandle.tools
    } else {
      const baseTools = await loadTools(request, toolToken, deadline.signal)
      tools = baseTools
      if (options?.case === 'extension') {
        const { guardQuantity, queryTool } = await import('./business_extension.mjs')
        const limit = options.business_extension.quantity_limit
        const receiptSchema = baseTools.find(tool => tool.name === 'receipt')?.parameters
        tools = baseTools.map(tool => guardQuantity(tool, limit, evidence, scope))
        tools.push(queryTool(receiptSchema, request.tools_url, { Authorization: `Bearer ${toolToken}`, 'Content-Type': 'application/json' }, evidence, scope, observations, deadline.signal))
      }
    }
    const model = {
      id: process.env.MODEL_NAME, name: process.env.MODEL_NAME,
      api: 'openai-completions', provider: 'harness-lab', baseUrl: process.env.MODEL_API_BASE_URL,
      reasoning: false, input: ['text'],
      // 这些值是本地初筛上限，不能视为已探明的模型能力。
      contextWindow: 65536, maxTokens: request.max_output_tokens,
      // 此处仅填写 Pi 必需的字段。实际费用以代理统计为准；
      // 下方明确将原生零费率标为占位值。
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      compat: { supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false, supportsUsageInStreaming: true, maxTokensField: 'max_tokens', supportsStrictMode: false },
    }
    const observeFetch = async (input, init) => {
      const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url)
      if (url.origin !== 'http://model-proxy:8082' || url.pathname !== '/v1/chat/completions' || url.search || url.username || url.password) throw new Error('模型传输试图访问非预期接口')
      deadline.signal.throwIfAborted()
      transportAttempts++
      appendFileSync(transportLog, encode({ type: 'provider_http_attempt', attempt: transportAttempts, method: init?.method ?? (input instanceof Request ? input.method : 'GET'), path: url.pathname, time: new Date().toISOString() }))
      return fetch(input, { ...init, signal: init?.signal ? AbortSignal.any([deadline.signal, init.signal]) : deadline.signal })
    }
    agent = new Agent({
      initialState: { model, systemPrompt: '使用所提供的工具完成用户任务。工具输入和输出来自共享实验服务。', tools, thinkingLevel: 'off' },
      sessionId: request.session_id,
      getApiKey: () => modelToken,
      streamFn: (activeModel, context, options) => streamSimple(activeModel, context, {
        ...options, apiKey: modelToken, maxTokens: request.max_output_tokens,
        maxRetries: request.max_retries, timeoutMs: request.timeout_seconds * 1000,
        fetch: observeFetch,
      }),
    })
    agent.subscribe(event => {
      appendFileSync(raw, encode(event))
      if (event.type === 'agent_end') nativeEnd = event
    })
    writeFileSync(resolve(request.output_dir, 'entrypoint.json'), encode({
      run_id: request.run_id, session_id: request.session_id, started_at: new Date(started).toISOString(),
      factory: '@earendil-works/pi-agent-core.Agent', provider_api: 'openai-completions', provenance,
      tool_names: tools.map(tool => tool.name), native_cost_rates: 'zero_placeholders_not_pricing',
      model_context_window: '65536_local_screening_ceiling_not_discovered_model_limit',
      state: 'in_memory_agent_state_with_application_observation_only',
      controls: { pause: 'not_implemented', resume: 'not_implemented', cancel: 'official_Agent_abort' },
      instance_boundary: 'one_Agent_object_in_one_process; sharing_process_is_possible_but_unverified',
    }, true))
    deadline.signal.throwIfAborted()
    await agent.prompt(request.prompt)
    await agent.waitForIdle()
    const last = agent.state.messages.findLast(message => message.role === 'assistant')
    const stop = last?.stopReason ?? 'missing_native_assistant'
    const completed = !deadline.signal.aborted && nativeEnd && !agent.state.isStreaming && agent.state.pendingToolCalls.size === 0 && stop === 'stop' && !agent.state.errorMessage
    summary = {
      status: completed ? 'completed' : timedOut ? 'timed_out' : deadline.signal.aborted ? 'interrupted' : ['error', 'aborted'].includes(stop) ? 'failed' : 'incomplete',
      stop_reason: timedOut ? 'active_timeout' : stop,
      final_response: last?.content?.filter(part => part.type === 'text').map(part => part.text).join('') ?? null,
      native_terminal: { observed_agent_end: Boolean(nativeEnd), assistant_stop_reason: stop, is_streaming: agent.state.isStreaming, pending_tool_calls: [...agent.state.pendingToolCalls], error_message: agent.state.errorMessage ?? last?.errorMessage ?? null },
      native_result: nativeEnd,
      native_cost_rates: 'zero_placeholders_not_pricing',
      provider_http_attempts: transportAttempts,
    }
  } catch (error) {
    agent?.abort()
    summary = { status: timedOut ? 'timed_out' : deadline.signal.aborted ? 'interrupted' : 'failed', stop_reason: timedOut ? 'active_timeout' : deadline.signal.aborted ? 'process_cancellation' : 'runtime_error', error: redact(error instanceof Error ? error.message : error), provider_http_attempts: transportAttempts }
  } finally {
    clearTimeout(timer)
    process.removeListener('SIGTERM', interrupt); process.removeListener('SIGINT', interrupt)
    if (browserHandle) {
      try { await browserHandle.close() }
      catch (error) { summary.cleanup_errors = [{ operation: 'browser_mcp_close', error: String(error?.message ?? error) }] }
    }
    if (request.candidate_options?.case === 'extension') {
      try {
        extensionDone = { state: aggregateBusinessState(scope, observations) }
      } catch (error) {
        summary.projection_error = String(error?.message ?? error)
      }
    }
    if (agent) saveState(request, agent, extensionDone)
    if (extensionDone && evidence) {
      const { writeProjection } = await import('./business_extension.mjs')
      writeProjection(request.output_dir, resolve(request.output_dir, 'state-snapshot.json'),
        extensionDone.state, scope, writeFileSync, readFileSync)
      evidence.finalize()
    }
  }
  summary.session_id = request.session_id
  summary.elapsed_seconds = (Date.now() - started) / 1000
  writeFileSync(resolve(request.output_dir, 'result.json'), encode(summary, true))
  process.stdout.write(encode({ status: summary.status, stop_reason: summary.stop_reason, session_id: request.session_id }))
  if (summary.status !== 'completed') process.exitCode = 1
}

main().catch(error => { process.stderr.write(redact(error instanceof Error ? error.message : error) + '\n'); process.exitCode = 1 })
