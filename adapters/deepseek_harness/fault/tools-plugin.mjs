/** 将可信 HTTP 工具投影到上游 DSH 工具注册表。 */
import { readFile } from 'node:fs/promises'
import { loadI23Prewrite } from './prewrite-gate.mjs'

export const name = 'harness-lab-http-tools'
export const inject = ['tools']

export async function mountHttpTools(ctx, options = {}) {
  const businessExtension = options?.businessExtension
  const endpoint = new URL(process.env.HARNESS_TOOLS_URL)
  if (endpoint.protocol !== 'http:' || endpoint.hostname !== 'tools' || endpoint.port !== '8081' || endpoint.pathname !== '/' || endpoint.search || endpoint.username || endpoint.password) {
    throw new Error('必须使用实验工具服务接口')
  }
  const token = (await readFile(process.env.HARNESS_TOOL_TOKEN_FILE, 'utf8')).trim()
  if (!token || /\s/u.test(token)) throw new Error('可信工具令牌文件无效')
  const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
  // 默认不存在；仅 I23 变体接收尝试绑定。
  const prewrite = await loadI23Prewrite(endpoint, headers.Authorization)
  const response = await fetch(new URL('/tools', endpoint), { headers, signal: AbortSignal.timeout(10_000) })
  if (!response.ok) throw new Error(`无法列出可信 HTTP 工具（${response.status}）`)
  const { tools } = await response.json()
  if (!Array.isArray(tools) || tools.length === 0) throw new Error('可信工具定义为空')
  if (businessExtension) {
    const expected = ['list_items', 'write', 'receipt', 'ledger', 'read_file', 'write_file', 'calculate_totals', 'sensitive_export']
    if (tools.length !== expected.length || new Set(tools.map(tool => tool?.name)).size !== expected.length || expected.some(name => !tools.some(tool => tool?.name === name))) throw new Error('扩展必须使用原始的八个可信工具')
  }
  const definitions = tools.map(tool => ({ ...tool, canonical_name: tool.name }))
  if (businessExtension) {
    const receipt = tools.find(tool => tool.name === 'receipt')
    definitions.push({ name: 'query_order_status', canonical_name: 'receipt',
      description: '查询当前作用域中某个 operation_id 的实际回执及 write_count。只读，不写入、重试或执行剩余任务步骤。',
      input_schema: structuredClone(receipt.input_schema) })
  }
  for (const tool of definitions) {
    if (!tool || typeof tool.name !== 'string' || typeof tool.description !== 'string' || typeof tool.input_schema !== 'object') throw new Error('可信工具定义无效')
    const carriesBusinessState = !!businessExtension && ['write', 'receipt'].includes(tool.canonical_name)
    ctx.tools.register({
      name: tool.name,
      description: tool.description,
      parameters: tool.input_schema,
      output: {
        schema: carriesBusinessState ? {
          type: 'object', required: ['result', 'business_state'], additionalProperties: false,
          properties: { result: { type: 'object', additionalProperties: true }, business_state: { type: 'object', additionalProperties: true } },
        } : { type: 'object', additionalProperties: true },
        render: (_arguments, value) => [{ type: 'text', text: JSON.stringify(carriesBusinessState ? value.result : value) }],
        ...(carriesBusinessState ? { presentationMeta: businessExtension.presentationMeta } : {}),
      },
      async execute(arguments_, execution) {
        const eventBase = { scope: businessExtension?.scope, call_id: String(execution.callId),
          tool: tool.canonical_name, exposed_tool: tool.name, arguments: arguments_ }
        const started = businessExtension?.append('http-tool-events.jsonl', {
          ...eventBase, time: new Date().toISOString(), phase: 'start', http_forwarded: false,
        })
        await businessExtension?.beforeForward(tool.canonical_name, arguments_, started)
        let result
        let payload
        let httpForwarded = false
        try {
          // 可选别名仅执行这一次原始回执调用。
          // 应用层不重试、不自动查询，也不替代 Agent 执行任务操作。
          const experimentHeaders = prewrite
            ? await prewrite.before(tool.canonical_name, arguments_, execution.callId, execution.signal)
            : {}
          httpForwarded = true
          result = await fetch(new URL('/call', endpoint), {
            method: 'POST', headers: { ...headers, ...experimentHeaders },
            body: JSON.stringify({ name: tool.canonical_name, arguments: arguments_ }),
            signal: execution.signal,
          })
          payload = await result.json()
        } catch (error) {
          const source = businessExtension?.append('http-tool-events.jsonl', {
            ...eventBase, time: new Date().toISOString(), phase: 'error', status: result?.status ?? null,
            http_forwarded: httpForwarded, error: error instanceof Error ? error.message : String(error),
          })
          await businessExtension?.observe(tool.canonical_name, arguments_, result?.status ?? null, null, source)
          throw error
        }
        const source = businessExtension?.append('http-tool-events.jsonl', {
          ...eventBase, time: new Date().toISOString(), phase: 'finish', status: result.status,
          http_forwarded: true, result: payload,
        })
        const businessState = await businessExtension?.observe(tool.canonical_name, arguments_, result.status, payload, source)
        if (!result.ok) throw new Error(JSON.stringify({ status: result.status, ...payload }))
        return carriesBusinessState ? { result: payload.result, business_state: businessState } : payload.result
      },
    })
  }
}

// I08 的 SDK profile 仍在原始宿主层挂载同一插件。
// I17 通过尚未发布 Agent 的作用域上下文调用此函数。
export async function apply(ctx) { await mountHttpTools(ctx) }
