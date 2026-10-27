/** 通过原生 MCP，将原始业务工具接入官方 TS SDK/CLI 并运行。 */
import { appendFileSync, existsSync, lstatSync, mkdirSync, readdirSync, readFileSync, readlinkSync, realpathSync, writeFileSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { verifySource } from './verify-source.mjs'

const sdkPath = '/opt/materials/packages/@openai/codex-sdk/dist/index.js'
const cliPath = '/opt/materials/packages/@openai/codex-linux-arm64/vendor/aarch64-unknown-linux-musl/bin/codex'
const toolNames = ['list_items', 'write', 'receipt', 'ledger', 'read_file', 'write_file', 'calculate_totals', 'sensitive_export']
const secrets = []
const redact = value => secrets.reduce((text, token) => text.replaceAll(token, '[redacted-local-token]'), String(value))
const encoded = value => redact(JSON.stringify(value, null, 2)) + '\n'

function trustedPath(value, directory = false) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('可信运行路径无效')
  const path = resolve(value)
  let current = '/'
  for (const part of path.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('可信路径不能包含链接')
  }
  if (directory) mkdirSync(path, { recursive: true, mode: 0o700 })
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
  const args = process.argv.slice(2)
  if (args.length !== 2 || args[0] !== '--request') throw new Error('用法：entrypoint --request /config/request.json')
  const request = JSON.parse(readFileSync(trustedPath(args[1]), 'utf8'))
  const keys = ['run_id', 'session_id', 'prompt', 'workspace', 'state_dir', 'output_dir', 'tools_url', 'tool_token_file', 'max_output_tokens', 'timeout_seconds', 'max_retries']
  if (!request || typeof request !== 'object' || Array.isArray(request) || Object.keys(request).length !== keys.length || keys.some(key => !Object.hasOwn(request, key))) throw new Error('可信运行请求的字段无效')
  for (const name of ['run_id', 'session_id']) if (typeof request[name] !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u.test(request[name]) || request[name].toLowerCase() === 'old') throw new Error('运行或会话标识无效')
  if (typeof request.prompt !== 'string' || !request.prompt || request.prompt.length > 65536) throw new Error('提示词不能为空，且长度不能超过限制')
  for (const [name, min, max] of [['max_output_tokens', 1, 16384], ['timeout_seconds', 1, 3600], ['max_retries', 0, 2]]) if (!Number.isSafeInteger(request[name]) || request[name] < min || request[name] > max) throw new Error('运行预算无效')
  const paths = ['workspace', 'state_dir', 'output_dir'].map(name => request[name] = trustedPath(request[name], true))
  if (paths.some((a, index) => paths.slice(index + 1).some(b => a === b || a.startsWith(b + '/') || b.startsWith(a + '/')))) throw new Error('工作区、状态和证据必须分别使用独立的目录树')
  trustedPath(request.tool_token_file)
  if (request.tools_url !== 'http://tools:8081') throw new Error('必须使用限定作用域的实验工具服务')
  return request
}

function stateFiles(directory) {
  const files = []
  const visit = (root, depth = 0) => {
    if (depth > 12 || files.length > 4096) throw new Error('原生状态清单的深度或文件数量超过限制')
    for (const entry of readdirSync(root, { withFileTypes: true })) {
      const path = resolve(root, entry.name)
      // 官方 CLI 会在自己的私有目录中创建可执行文件别名。
      // 只记录链接本身，不把链接目标当作可信状态读取。
      if (entry.isSymbolicLink()) files.push({ path: path.slice(directory.length + 1), type: 'symlink', target: readlinkSync(path) })
      else if (entry.isDirectory()) visit(path, depth + 1)
      else if (entry.isFile()) files.push({ path: path.slice(directory.length + 1), size: lstatSync(path).size })
    }
  }
  visit(directory)
  return files
}

async function main() {
  const provenance = verifySource()
  const request = readRequest()
  const started = Date.now()
  const controller = new AbortController()
  let timedOut = false
  const timer = setTimeout(() => { timedOut = true; controller.abort(new Error('任务执行超时')) }, request.timeout_seconds * 1000)
  const interrupt = () => controller.abort(new Error('收到容器信号'))
  process.once('SIGTERM', interrupt)
  process.once('SIGINT', interrupt)
  const modelProbe = process.env.MODEL_NAME === 'probe-model'
  const summary = { status: 'failed', stop_reason: 'runtime_error', session_id: request.session_id, phase: modelProbe ? 'mock_model_budget_probe' : 'common_tools_task', unified_tools: modelProbe ? 'intentionally_not_exposed_in_mock_budget_probe' : 'original_eight_HTTP_tools_via_official_stdio_MCP_configuration;_task_result_requires_independent_grading', builtin_tool_isolation: 'public_shell_goal_image_flags_off;_remaining_tool_surface_requires_runtime_audit', output_budget: { requested_max_output_tokens: request.max_output_tokens, publicly_configurable: false, enforcement: 'not_available_in_fixed_SDK_CLI_public_options;_proxy_counts_requests_only' } }
  let thread, exhausted = false, terminal = null, turnStarted = false
  const errors = [], warnings = [], messages = [], toolErrors = [], mcpCalls = [], pendingItems = new Set()
  const nativeHome = trustedPath(resolve(request.state_dir, 'codex-home'), true)
  try {
    if (process.platform !== 'linux' || process.arch !== 'arm64') throw new Error('候选运行时要求 Linux arm64 环境')
    if (process.env.MODEL_API_BASE_URL !== 'http://model-proxy:8082/v1' || !process.env.MODEL_NAME || /\s/u.test(process.env.MODEL_NAME)) throw new Error('候选必须使用计数代理，以及可信编排指定的模型')
    if (process.env.MODEL_API_KEY) throw new Error('候选只能接收本地运行令牌文件')
    if (readdirSync(nativeHome).length) throw new Error('此入口要求全新的 CODEX_HOME，尚未实现恢复功能')
    const token = tokenFile(process.env.MODEL_RUN_TOKEN_FILE)
    const cliVersion = readFileSync('/opt/materials/CLI_VERSION.txt', 'utf8').trim()
    if (cliVersion !== 'codex-cli 0.163.0') throw new Error('实际 CLI 版本与发布的 SDK 配套版本不符')
    const config = {
      model_provider: 'harness',
      model_providers: { harness: { name: 'Harness 计数代理', base_url: process.env.MODEL_API_BASE_URL, env_key: 'HARNESS_MODEL_RUN_TOKEN', wire_api: 'responses', requires_openai_auth: false, supports_websockets: false, request_max_retries: request.max_retries, stream_max_retries: 0, stream_idle_timeout_ms: request.timeout_seconds * 1000 } },
      features: { shell_tool: false, view_image: false, goals: false, sleep_tool: false, code_mode: false, js_repl: false, multi_agent: false, browser_use: false, apps: false, remote_models: false, api_key_model_discovery: false, standalone_web_search: false },
      tools: { update_plan: { enabled: false }, experimental_request_user_input: { enabled: false } },
      analytics: { enabled: false },
      otel: { exporter: 'none', trace_exporter: 'none', metrics_exporter: 'none' },
      project_doc_max_bytes: 0,
    }
    if (!modelProbe) {
      const toolToken = tokenFile(request.tool_token_file)
      const discovery = await fetch(request.tools_url + '/tools', { headers: { Authorization: `Bearer ${toolToken}` }, signal: AbortSignal.any([controller.signal, AbortSignal.timeout(10_000)]) })
      if (!discovery.ok) throw new Error(`无法发现原始业务工具 (${discovery.status})`)
      const tools = (await discovery.json()).tools
      if (!Array.isArray(tools) || tools.length !== toolNames.length || new Set(tools.map(tool => tool.name)).size !== toolNames.length || tools.some(tool => !toolNames.includes(tool.name) || typeof tool.description !== 'string' || !tool.input_schema || tool.input_schema.type !== 'object' || tool.input_schema.additionalProperties !== false)) throw new Error('必须提供八个原始可信工具定义')
      const bindingPath = resolve(request.state_dir, 'codex-http-tools.json')
      writeFileSync(bindingPath, JSON.stringify({ run_id: request.run_id, session_id: request.session_id, tools_url: request.tools_url, token_file: request.tool_token_file, output_dir: request.output_dir, deadline_at: started + request.timeout_seconds * 1000, tools }, null, 2) + '\n', { mode: 0o600 })
      writeFileSync(resolve(request.output_dir, 'tool-surface.json'), encoded({ source: 'authenticated_HTTP_GET_/tools', tools, projection: 'MCP name/description/inputSchema;_no_schema_rewrite_or_business_workflow' }))
      config.mcp_servers = { harness: {
        command: '/usr/local/bin/node', args: [resolve(import.meta.dirname, 'http-mcp-bridge.mjs'), '--binding', bindingPath],
        cwd: request.workspace, enabled: true, required: true, startup_readiness: 'connection',
        startup_timeout_sec: 20, tool_timeout_sec: 35, enabled_tools: toolNames,
        // `auto` 仍会参考 MCP 注解；没有注解的工具可能需要
        // 审批。此处明确放行已固定的可信业务工具范围。
        default_tools_approval_mode: 'approve', supports_parallel_tool_calls: true,
      } }
    }
    writeFileSync(resolve(request.output_dir, 'entrypoint.json'), encoded({
      run_id: request.run_id, session_id: request.session_id, started_at: new Date(started).toISOString(), provenance, cli_version: cliVersion,
      factory: 'official_@openai/codex-sdk_Codex.startThread.runStreamed', codex_path_override: cliPath,
      phase: summary.phase, selected_model: process.env.MODEL_NAME, native_wire_api: 'responses', model_protocol_conversion: false,
      native_configuration: config, native_home: nativeHome,
      output_budget: summary.output_budget, unified_tools: summary.unified_tools, builtin_tool_isolation: summary.builtin_tool_isolation,
      tool_boundary: modelProbe ? 'model_budget_probe_only;_no_unified_task_claim' : 'official_MCP_business_tools;_public_shell_goal_view_image_websearch_flags_off;_tool_search_and_MCP_resource_metadata_may_remain',
      controls: { cancel: 'official_TurnOptions_AbortSignal', pause: 'not_implemented', resume: 'not_verified' },
      instance_boundary: 'official_SDK_spawns_one_CLI_process_for_this_turn;_sharing_and_tenant_binding_unverified',
    }))
    const { Codex } = await import(sdkPath)
    const codex = new Codex({
      codexPathOverride: cliPath, baseUrl: process.env.MODEL_API_BASE_URL, apiKey: token, config,
      env: { PATH: '/usr/local/bin:/usr/bin:/bin', CODEX_HOME: nativeHome, TMPDIR: '/tmp', HARNESS_MODEL_RUN_TOKEN: token, DO_NOT_TRACK: '1', OTEL_SDK_DISABLED: 'true' },
    })
    thread = codex.startThread({ model: process.env.MODEL_NAME, workingDirectory: request.workspace, skipGitRepoCheck: true, sandboxMode: 'read-only', approvalPolicy: 'never', webSearchMode: 'disabled', networkAccessEnabled: false })
    const { events } = await thread.runStreamed(request.prompt, { signal: controller.signal })
    for await (const event of events) {
      appendFileSync(resolve(request.output_dir, 'raw-events.jsonl'), redact(JSON.stringify(event)) + '\n')
      if (event.type === 'turn.started') turnStarted = true
      if (event.type === 'turn.completed' || event.type === 'turn.failed') terminal = event
      if (event.type === 'error') errors.push(event)
      if (event.type === 'turn.failed') errors.push(event.error)
      if (event.type === 'item.started' || event.type === 'item.updated') pendingItems.add(event.item.id)
      if (event.type === 'item.completed') {
        pendingItems.delete(event.item.id)
        if (event.item.type === 'agent_message') messages.push(event.item.text)
        if (event.item.type === 'error' && !turnStarted && /^Model metadata for `[^`]+` not found\. Defaulting to fallback metadata; this can degrade performance and cause issues\.$/u.test(event.item.message ?? '')) warnings.push(event.item)
        else if (event.item.type === 'error') errors.push(event.item)
        if (event.item.type === 'mcp_tool_call') {
          mcpCalls.push(event.item)
          if (event.item.status === 'failed' || event.item.error) toolErrors.push(event.item)
          const businessTool = event.item.server === 'harness' && toolNames.includes(event.item.tool)
          const nativeMetadata = ['list_mcp_resources', 'list_mcp_resource_templates', 'read_mcp_resource'].includes(event.item.tool)
          if (modelProbe || (!businessTool && !nativeMetadata)) errors.push({ type: 'unexpected_mcp_tool_boundary', item: event.item })
        } else if (event.item.status === 'failed') errors.push(event.item)
        if (['command_execution', 'file_change', 'web_search'].includes(event.item.type)) errors.push({ type: 'unexpected_builtin_task_tool', item: event.item })
      }
    }
    exhausted = true
    const completed = turnStarted && terminal?.type === 'turn.completed' && errors.length === 0 && pendingItems.size === 0 && !controller.signal.aborted
    Object.assign(summary, { status: completed ? 'completed' : errors.length ? 'failed' : 'incomplete', stop_reason: terminal?.type ?? 'missing_native_turn_terminal', final_response: messages.at(-1) ?? null, usage: terminal?.usage ?? null })
  } catch (error) {
    Object.assign(summary, { status: timedOut ? 'timed_out' : controller.signal.aborted ? 'interrupted' : 'failed', stop_reason: timedOut ? 'active_timeout' : controller.signal.aborted ? 'process_cancellation' : 'runtime_error', error_type: error?.name ?? 'Error', error: redact(error?.message ?? error) })
  } finally {
    clearTimeout(timer)
    process.removeListener('SIGTERM', interrupt)
    process.removeListener('SIGINT', interrupt)
    try {
      writeFileSync(resolve(request.output_dir, 'state-snapshot.json'), encoded({ thread_id: thread?.id ?? null, native_home: nativeHome, files: stateFiles(nativeHome), kind: 'native_state_file_inventory;_not_a_resume_capability_claim' }))
    } catch (error) {
      summary.evidence_error = redact(error?.message ?? error)
      if (summary.status === 'completed') Object.assign(summary, { status: 'failed', stop_reason: 'state_evidence_failure' })
    }
  }
  summary.thread_id = thread?.id ?? null
  summary.native_terminal = { iterator_exhausted: exhausted, turn_started: turnStarted, terminal_event: terminal, errors, warnings, pending_item_ids: [...pendingItems], native_finish_reason: 'not_exposed_by_TS_SDK_turn_event_schema' }
  summary.native_mcp_calls = mcpCalls
  summary.native_tool_errors = toolErrors
  summary.elapsed_seconds = (Date.now() - started) / 1000
  writeFileSync(resolve(request.output_dir, 'result.json'), encoded(summary))
  process.stdout.write(JSON.stringify({ status: summary.status, stop_reason: summary.stop_reason, session_id: request.session_id }) + '\n')
  if (summary.status !== 'completed') process.exitCode = 1
}

try { await main() } catch (error) { process.stderr.write(redact(error?.message ?? error) + '\n'); process.exitCode = 1 }
