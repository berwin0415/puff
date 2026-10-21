/** Project the trusted HTTP tools into the upstream DSH tool registry. */
import { readFile } from 'node:fs/promises'
import { loadI23Prewrite } from './prewrite-gate.mjs'

export const name = 'harness-lab-http-tools'
export const inject = ['tools']

export async function mountHttpTools(ctx, options = {}) {
  const businessExtension = options?.businessExtension
  const endpoint = new URL(process.env.HARNESS_TOOLS_URL)
  if (endpoint.protocol !== 'http:' || endpoint.hostname !== 'tools' || endpoint.port !== '8081' || endpoint.pathname !== '/' || endpoint.search || endpoint.username || endpoint.password) {
    throw new Error('Expected the experiment tools service endpoint')
  }
  const token = (await readFile(process.env.HARNESS_TOOL_TOKEN_FILE, 'utf8')).trim()
  if (!token || /\s/u.test(token)) throw new Error('Invalid trusted tools token file')
  const headers = { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' }
  // Absent by default; only the I23 variant receives an attempt binding.
  const prewrite = await loadI23Prewrite(endpoint, headers.Authorization)
  const response = await fetch(new URL('/tools', endpoint), { headers, signal: AbortSignal.timeout(10_000) })
  if (!response.ok) throw new Error(`Cannot list trusted HTTP tools (${response.status})`)
  const { tools } = await response.json()
  if (!Array.isArray(tools) || tools.length === 0) throw new Error('Empty trusted tool definitions')
  if (businessExtension) {
    const expected = ['list_items', 'write', 'receipt', 'ledger', 'read_file', 'write_file', 'calculate_totals', 'sensitive_export']
    if (tools.length !== expected.length || new Set(tools.map(tool => tool?.name)).size !== expected.length || expected.some(name => !tools.some(tool => tool?.name === name))) throw new Error('Extension requires the eight original trusted tools')
  }
  const definitions = tools.map(tool => ({ ...tool, canonical_name: tool.name }))
  if (businessExtension) {
    const receipt = tools.find(tool => tool.name === 'receipt')
    definitions.push({ name: 'query_order_status', canonical_name: 'receipt',
      description: 'Query actual receipts and write_count for one operation_id in the current scope. Read-only: no writes, retries, or remaining task steps.',
      input_schema: structuredClone(receipt.input_schema) })
  }
  for (const tool of definitions) {
    if (!tool || typeof tool.name !== 'string' || typeof tool.description !== 'string' || typeof tool.input_schema !== 'object') throw new Error('Invalid trusted tool definition')
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
          // The optional alias makes exactly this one original receipt call.
          // No application retry, automatic query, or substitute task action.
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

// I08's SDK profile still mounts the same plugin in its original host layer.
// I17 invokes this function through the unpublished Agent's scoped context.
export async function apply(ctx) { await mountHttpTools(ctx) }
