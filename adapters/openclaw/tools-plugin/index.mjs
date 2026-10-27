/** 仅转换可信工具定义和参数；循环由上游负责。 */
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { appendEvidence, readBrowserCredentials, redactor } from './detailed-options.mjs'
import { createBrowserClient } from './browser-client.mjs'
import { BUSINESS_NAMESPACE, createBusinessState } from './business-state.mjs'

export default {
  id: 'harness-lab-tools',
  register(api) {
    // OpenClaw 注册是同步的；可信启动器在加载插件前仅获取
    // 元数据，不获取目录数据或任务答案。
    const binding = JSON.parse(readFileSync(process.env.HARNESS_TOOL_BINDING_FILE, 'utf8'))
    if (binding.tools_url !== 'http://tools:8081' || binding.agent_id !== 'lab-a' || typeof binding.session_key !== 'string' || !['http', 'browser'].includes(binding.transport)) throw new Error('可信工具绑定无效')
    const credentials = binding.browser ? readBrowserCredentials(binding.browser.credentials_file) : null
    const token = binding.transport === 'http' ? readFileSync(binding.tool_token_file, 'utf8').trim() : null
    if (binding.transport === 'http' && (!token || /\s/u.test(token))) throw new Error('可信工具令牌无效')
    const redact = redactor([token, credentials?.password, process.env.HARNESS_MODEL_RUN_TOKEN, process.env.HARNESS_GATEWAY_TOKEN])
    const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
    const record = (type, event, context) => appendEvidence(binding.events_path, { time: new Date().toISOString(), type, event, context }, redact)
    if (binding.business_extension) {
      if (binding.transport !== 'http' || !binding.scope || binding.business_extension.quantity_limit !== 3) throw new Error('冻结的业务扩展绑定无效')
      api.session.state.registerSessionExtension({ namespace: BUSINESS_NAMESPACE, description: '当前作用域内的业务操作和实际回执确认；仅使用原生会话数据' })
      api.on('before_tool_call', (event, context) => {
        const bound = context.agentId === binding.agent_id && context.sessionKey === binding.session_key && Boolean(context.sessionId)
        const denied = !bound || (typeof event.params?.quantity === 'number' && event.params.quantity > binding.business_extension.quantity_limit)
        const reason = !bound ? 'business_policy_session_mismatch' : denied ? 'quantity_limit_exceeded' : 'quantity_within_limit'
        const nativeResult = denied ? { block: true, blockReason: reason } : null
        const source = appendEvidence(binding.events_path, { time: new Date().toISOString(), type: 'business_quantity_policy', phase: 'guard', scope: binding.scope, tool: 'write', native_tool: event.toolName, call_id: event.toolCallId ?? context.toolCallId ?? null, arguments: event.params, operation_id: event.params?.operation_id, quantity: event.params?.quantity, quantity_limit: 3, decision: denied ? 'deny' : 'allow', reason, approval_required: false, status: denied ? 403 : 200, status_source: 'candidate_policy_classification;native_hook_returns_block_not_HTTP', native_hook_result: nativeResult, http_forwarded: false, context }, redact)
        const decision = { time: new Date().toISOString(), scope: binding.scope, tool: 'write', native_tool: event.toolName, call_id: event.toolCallId ?? context.toolCallId ?? null, operation_id: event.params?.operation_id, quantity: event.params?.quantity, quantity_limit: 3, decision: denied ? 'deny' : 'allow', reason, approval_required: false, layer: 'candidate_execution', native_layer: 'before_tool_call', http_forwarded: false, source }
        appendEvidence(resolve(dirname(binding.events_path), 'policy-decisions.jsonl'), decision, redact)
        appendEvidence(resolve(dirname(binding.events_path), 'business-rule-decisions.jsonl'), decision, redact)
        if (denied) return { block: true, blockReason: reason }
      }, { matcher: ['harness_write'], priority: 50 })
    }
    let browserClient
    for (const hook of ['llm_input', 'llm_output', 'agent_end']) api.on(hook, (event, context) => { record(hook, event, context) })
    for (const hook of ['before_tool_call', 'after_tool_call']) api.on(hook, (event, context) => { record(hook, event, context) }, { matcher: binding.tools.map(tool => `harness_${tool.name}`) })
    const factory = context => {
      // 此初始变体只有一个可信会话；调用者或模型提供的
      // 参数都不能改变接口、凭证或作用域。
      if (context.agentId !== binding.agent_id || context.sessionKey !== binding.session_key || !context.sessionId) {
        record('tool_binding_refused', {}, { agentId: context.agentId, sessionKey: context.sessionKey, sessionId: context.sessionId })
        return null
      }
      const businessState = binding.business_extension ? createBusinessState(api, binding, context, record, redact) : null
      return binding.tools.map(tool => ({
        name: `harness_${tool.name}`,
        label: tool.name,
        description: tool.description,
        parameters: tool.input_schema,
        async execute(callId, arguments_, signal) {
          if (binding.transport === 'browser') {
            browserClient ??= createBrowserClient({ browser: binding.browser, scope: binding.scope, outputDir: dirname(binding.events_path), workspace: binding.workspace, redact, deadlineAt: binding.deadline_at }, signal)
            const result = await (await browserClient).call(tool.name, arguments_, String(callId), signal)
            // 在 details 中保留原生 content 和 isError 结果；此处
            // 不重新解释页面内容，也不决定下一个工具调用。
            return { content: result.content, details: { native_mcp_result: result, ...(result.isError ? { status: 'error' } : {}) } }
          }
          if (businessState) await businessState.before(tool.name, arguments_, callId)
          // 新增查询只是一次原子回执请求，不是完整流程。
          const canonicalTool = tool.name === 'query_order_status' ? 'receipt' : tool.name
          const response = await fetch(binding.tools_url + '/call', {
            method: 'POST', headers, body: JSON.stringify({ name: canonicalTool, arguments: arguments_ }),
            signal: signal ? AbortSignal.any([signal, AbortSignal.timeout(30_000)]) : AbortSignal.timeout(30_000),
          })
          const payload = await response.json()
          const source = record('harness_http_tool', { phase: 'finish', call_id: String(callId), tool: tool.name, canonical_tool: canonicalTool, arguments: arguments_, status: response.status, payload }, { agentId: context.agentId, sessionKey: context.sessionKey, sessionId: context.sessionId })
          const httpEvent = { time: new Date().toISOString(), phase: 'finish', scope: binding.scope, call_id: String(callId), tool: canonicalTool, exposed_tool: tool.name, arguments: arguments_, status: response.status, result: payload, source }
          const httpSource = businessState ? appendEvidence(resolve(dirname(binding.events_path), 'http-tool-events.jsonl'), httpEvent, redact) : null
          if (binding.scope && tool.name === 'sensitive_export' && response.status === 403 && payload.fixture_status === 403 && payload.details?.reason === 'sensitive_operation_denied' && payload.details?.approval_required === false) {
            appendEvidence(resolve(dirname(binding.events_path), 'policy-decisions.jsonl'), {
              time: new Date().toISOString(), scope: binding.scope, call_id: String(callId),
              operation_id: arguments_.operation_id, decision: 'deny', reason: 'sensitive_operation_denied',
              approval_required: false, layer: 'tool_execution', status: 403, fixture_status: 403, source,
            }, redact)
          }
          if (!response.ok) throw new Error(JSON.stringify({ status: response.status, ...payload }))
          if (businessState) await businessState.after(tool.name, arguments_, callId, httpEvent, httpSource)
          return { content: [{ type: 'text', text: JSON.stringify(payload.result) }], details: payload.result }
        },
      }))
    }
    api.registerTool(binding.business_extension ? { contextVersion: 2, create: factory } : factory, { names: binding.tools.map(tool => `harness_${tool.name}`), workspaceAccess: false })
  },
}
