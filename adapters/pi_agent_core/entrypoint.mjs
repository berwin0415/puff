/** Thin host of the official Pi Agent Core loop and OpenAI chat adapter. */
import { appendFileSync, existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { verifySource } from './verify-source.mjs'

const secrets = []
const redact = value => secrets.reduce((text, token) => text.replaceAll(token, '[redacted-local-token]'), String(value))
const encode = (value, pretty = false) => redact(JSON.stringify(value, (_key, item) => item instanceof Set ? [...item] : item, pretty ? 2 : undefined)) + '\n'

function trustedPath(value, directory = false) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('Invalid trusted run path')
  const path = resolve(value)
  let current = '/'
  for (const part of path.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('Trusted paths must not contain symbolic links')
  }
  if (directory) mkdirSync(path, { recursive: true })
  if (realpathSync(path) !== path) throw new Error('Trusted run path resolution changed')
  return path
}

function tokenFile(filename) {
  const path = trustedPath(filename)
  if (!lstatSync(path).isFile() || lstatSync(path).size > 8192) throw new Error('Expected a bounded local token file')
  const token = readFileSync(path, 'utf8').trim()
  if (!token || /\s/u.test(token)) throw new Error('Expected a nonempty local token')
  secrets.push(token)
  return token
}

function readRequest() {
  if (process.argv.length !== 4 || process.argv[2] !== '--request') throw new Error('Usage: entrypoint --request /config/request.json')
  const request = JSON.parse(readFileSync(trustedPath(process.argv[3]), 'utf8'))
  const keys = ['run_id', 'session_id', 'prompt', 'workspace', 'state_dir', 'output_dir', 'tools_url', 'tool_token_file', 'max_output_tokens', 'timeout_seconds', 'max_retries']
  if (!request || typeof request !== 'object' || Array.isArray(request) || Object.keys(request).length !== keys.length || keys.some(key => !Object.hasOwn(request, key))) throw new Error('Invalid trusted run request fields')
  for (const key of ['run_id', 'session_id']) if (typeof request[key] !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u.test(request[key]) || request[key].toLowerCase() === 'old') throw new Error('Invalid run/session identifier')
  if (typeof request.prompt !== 'string' || !request.prompt || request.prompt.length > 65536) throw new Error('Expected a bounded task prompt')
  for (const [key, minimum, maximum] of [['max_output_tokens', 1, 16384], ['timeout_seconds', 1, 3600], ['max_retries', 0, 2]]) if (!Number.isSafeInteger(request[key]) || request[key] < minimum || request[key] > maximum) throw new Error('Invalid run budget')
  for (const key of ['workspace', 'state_dir', 'output_dir']) request[key] = trustedPath(request[key], true)
  const paths = [request.workspace, request.state_dir, request.output_dir]
  if (paths.some((a, index) => paths.slice(index + 1).some(b => a === b || a.startsWith(b + '/') || b.startsWith(a + '/')))) throw new Error('Workspace, state and evidence must use separate directory trees')
  request.tool_token_file = trustedPath(request.tool_token_file)
  if (request.tools_url !== 'http://tools:8081') throw new Error('Expected the scoped experiment tools service')
  return request
}

async function loadTools(request, token, deadlineSignal) {
  const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
  const response = await fetch(new URL('/tools', request.tools_url), { headers, signal: AbortSignal.any([deadlineSignal, AbortSignal.timeout(10_000)]) })
  if (!response.ok) throw new Error(`Cannot list trusted HTTP tools (${response.status})`)
  const payload = await response.json()
  if (!payload || !Array.isArray(payload.tools) || payload.tools.length === 0) throw new Error('Empty trusted tool definitions')
  const seen = new Set()
  return payload.tools.map(tool => {
    if (!tool || typeof tool.name !== 'string' || !/^[A-Za-z][A-Za-z0-9_]*$/u.test(tool.name) || seen.has(tool.name) || typeof tool.description !== 'string' || !tool.input_schema || tool.input_schema.type !== 'object') throw new Error('Invalid or duplicate trusted tool definition')
    seen.add(tool.name)
    return {
      name: tool.name,
      label: tool.name,
      description: tool.description,
      parameters: tool.input_schema,
      async execute(_toolCallId, arguments_, signal) {
        // No automatic HTTP tool retry; committed writes can lose their receipt.
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

function saveState(request, agent) {
  // This is an application-owned observation, not a durable native checkpoint.
  const value = { persistence: 'application_written_observation_only', session_id: request.session_id, ...agent.state }
  const path = resolve(request.state_dir, 'observed-state.json')
  writeFileSync(path + '.tmp', encode(value, true))
  renameSync(path + '.tmp', path)
  writeFileSync(resolve(request.output_dir, 'state-snapshot.json'), encode(value, true))
}

async function main() {
  const provenance = verifySource()
  const request = readRequest()
  if (process.env.MODEL_API_BASE_URL !== 'http://model-proxy:8082/v1' || !process.env.MODEL_NAME) throw new Error('Candidate requires the counting proxy and selected model')
  const modelToken = process.env.MODEL_API_KEY || tokenFile(process.env.MODEL_RUN_TOKEN_FILE)
  if (process.env.MODEL_API_KEY) secrets.push(modelToken)
  const toolToken = tokenFile(request.tool_token_file)
  const { Agent, streamSimple } = await import('/opt/pi-build/packages/ai/pi-core.bundle.mjs')
  const started = Date.now()
  const deadline = new AbortController()
  let timedOut = false, agent, nativeEnd = null, transportAttempts = 0
  const raw = resolve(request.output_dir, 'raw-events.jsonl')
  writeFileSync(raw, '')
  const transportLog = resolve(request.output_dir, 'transport-events.jsonl')
  writeFileSync(transportLog, '')
  const interrupt = () => { deadline.abort(new Error('Process cancellation requested')); agent?.abort() }
  process.on('SIGTERM', interrupt); process.on('SIGINT', interrupt)
  const timer = setTimeout(() => { timedOut = true; deadline.abort(new Error('Active task deadline reached')); agent?.abort() }, request.timeout_seconds * 1000)
  let summary
  try {
    if (existsSync(resolve(request.state_dir, 'observed-state.json'))) throw new Error('This screening entry starts fresh sessions only; observed messages are not an implemented resume protocol')
    const tools = await loadTools(request, toolToken, deadline.signal)
    const model = {
      id: process.env.MODEL_NAME, name: process.env.MODEL_NAME,
      api: 'openai-completions', provider: 'harness-lab', baseUrl: process.env.MODEL_API_BASE_URL,
      reasoning: false, input: ['text'],
      // These are local screening ceilings, not a discovered model capability.
      contextWindow: 65536, maxTokens: request.max_output_tokens,
      // Required Pi schema fields only. Authoritative cost comes from the proxy;
      // native zero cost rates are explicitly marked as placeholders below.
      cost: { input: 0, output: 0, cacheRead: 0, cacheWrite: 0 },
      compat: { supportsStore: false, supportsDeveloperRole: false, supportsReasoningEffort: false, supportsUsageInStreaming: true, maxTokensField: 'max_tokens', supportsStrictMode: false },
    }
    const observeFetch = async (input, init) => {
      const url = new URL(typeof input === 'string' || input instanceof URL ? input : input.url)
      if (url.origin !== 'http://model-proxy:8082' || url.pathname !== '/v1/chat/completions' || url.search || url.username || url.password) throw new Error('Model transport attempted an unexpected endpoint')
      deadline.signal.throwIfAborted()
      transportAttempts++
      appendFileSync(transportLog, encode({ type: 'provider_http_attempt', attempt: transportAttempts, method: init?.method ?? (input instanceof Request ? input.method : 'GET'), path: url.pathname, time: new Date().toISOString() }))
      return fetch(input, { ...init, signal: init?.signal ? AbortSignal.any([deadline.signal, init.signal]) : deadline.signal })
    }
    agent = new Agent({
      initialState: { model, systemPrompt: 'Use the provided tools to carry out the user task. Tool input and output come from the shared experiment service.', tools, thinkingLevel: 'off' },
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
    if (agent) saveState(request, agent)
  }
  summary.session_id = request.session_id
  summary.elapsed_seconds = (Date.now() - started) / 1000
  writeFileSync(resolve(request.output_dir, 'result.json'), encode(summary, true))
  process.stdout.write(encode({ status: summary.status, stop_reason: summary.stop_reason, session_id: request.session_id }))
  if (summary.status !== 'completed') process.exitCode = 1
}

main().catch(error => { process.stderr.write(redact(error instanceof Error ? error.message : error) + '\n'); process.exitCode = 1 })
