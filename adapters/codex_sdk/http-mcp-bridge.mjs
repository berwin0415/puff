/** 为八个原始、限定作用域的 HTTP 工具提供最小 stdio MCP 传输桥。
 * 此桥不执行 Agent、模型、业务流程、计算、文件写入或 HTTP 重试。
 */
import { appendFileSync, existsSync, lstatSync, readFileSync, realpathSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { createInterface } from 'node:readline'

const allowed = ['list_items', 'write', 'receipt', 'ledger', 'read_file', 'write_file', 'calculate_totals', 'sensitive_export']
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)

function trustedPath(value) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('可信桥接路径无效')
  const path = resolve(value)
  let current = '/'
  for (const part of path.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('桥接路径不能经过链接')
  }
  if (realpathSync(path) !== path) throw new Error('桥接路径的解析结果发生变化')
  return path
}

async function main() {
  const args = process.argv.slice(2)
  if (args.length !== 2 || args[0] !== '--binding') throw new Error('必须通过 --binding 指定可信文件')
  const bindingPath = trustedPath(args[1])
  if (!lstatSync(bindingPath).isFile() || lstatSync(bindingPath).size > 262144) throw new Error('桥接绑定文件必须为普通文件，且大小不能超过限制')
  const binding = JSON.parse(readFileSync(bindingPath, 'utf8'))
  if (!object(binding) || binding.tools_url !== 'http://tools:8081' || !Number.isSafeInteger(binding.deadline_at) || typeof binding.run_id !== 'string' || typeof binding.session_id !== 'string') throw new Error('可信桥接绑定无效')
  const tools = binding.tools
  if (!Array.isArray(tools) || tools.length !== allowed.length || new Set(tools.map(tool => tool.name)).size !== allowed.length || tools.some(tool => !allowed.includes(tool.name) || typeof tool.description !== 'string' || !object(tool.input_schema) || tool.input_schema.type !== 'object' || tool.input_schema.additionalProperties !== false)) throw new Error('必须提供八个原始 HTTP 工具的结构定义')
  // 业务扩展：可选的数量规则与回执查询工具。缺省时行为完全不变。
  const extension = binding.business_extension ?? null
  if (extension !== null) {
    if (!object(extension) || !Number.isSafeInteger(extension.quantity_limit) || extension.quantity_limit !== 3
        || !object(extension.scope) || typeof extension.receipt_schema !== 'object') throw new Error('可信业务扩展绑定无效')
  }
  const queryTool = 'query_order_status'
  const listed = extension
    ? [...tools, { name: queryTool, description: '查询指定操作编号的真实回执与实际写入次数。只发起一次查询，不代办后续步骤。', input_schema: extension.receipt_schema }]
    : tools
  const tokenPath = trustedPath(binding.token_file)
  if (!lstatSync(tokenPath).isFile() || lstatSync(tokenPath).size > 8192) throw new Error('工具令牌文件必须为普通文件，且大小不能超过限制')
  const token = readFileSync(tokenPath, 'utf8').trim()
  if (!token || /\s/u.test(token)) throw new Error('可信工具令牌无效')
  const outputDir = trustedPath(binding.output_dir)
  if (!lstatSync(outputDir).isDirectory()) throw new Error('必须提供证据目录')
  const redact = value => String(value).replaceAll(token, '[redacted-local-token]')
  const log = (type, record) => appendFileSync(resolve(outputDir, 'mcp-bridge-events.jsonl'), redact(JSON.stringify({ time: new Date().toISOString(), run_id: binding.run_id, session_id: binding.session_id, type, ...record })) + '\n', { mode: 0o600 })
  const send = message => {
    // 响应原文保持不变，仅对留存的审计副本脱敏。
    log('stdio_outbound', { message })
    process.stdout.write(JSON.stringify(message) + '\n')
  }
  const respond = (id, result) => send({ jsonrpc: '2.0', id, result })
  const reject = (id, code, message) => send({ jsonrpc: '2.0', id, error: { code, message } })
  const active = new Map()
  let initialized = false
  let ready = false
  const remaining = () => {
    const value = Math.min(30_000, binding.deadline_at - Date.now())
    if (value <= 0) throw new Error('发送 HTTP 请求前已达到任务执行时限')
    return value
  }

  async function dispatch(message) {
    if (!object(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string') {
      reject(message?.id ?? null, -32600, 'JSON-RPC 请求无效')
      return
    }
    const hasId = Object.hasOwn(message, 'id')
    log('stdio_inbound', { message })
    if (message.method === 'notifications/cancelled') {
      active.get(message.params?.requestId)?.abort(new Error('原生 MCP 客户端已取消 HTTP 调用；已发送的写入是否生效仍不确定'))
      return
    }
    if (!hasId) {
      if (message.method === 'notifications/initialized' && initialized) ready = true
      return
    }
    if (!(typeof message.id === 'string' || Number.isSafeInteger(message.id))) {
      reject(null, -32600, 'JSON-RPC 请求标识无效')
      return
    }
    if (message.method === 'initialize') {
      if (initialized || !object(message.params) || typeof message.params.protocolVersion !== 'string') {
        reject(message.id, -32602, '初始化无效或重复')
        return
      }
      initialized = true
      // 此服务仅提供稳定的工具子集，不协商
      // 采样、信息征询、资源、任务、提示词或工作流扩展。
      respond(message.id, { protocolVersion: '2025-03-26', capabilities: { tools: { listChanged: false } }, serverInfo: { name: 'harness-lab-original-http-tools', version: '1.0.0' } })
      return
    }
    if (!ready) {
      reject(message.id, -32002, 'MCP 初始化尚未完成')
      return
    }
    if (message.method === 'ping') { respond(message.id, {}); return }
    if (message.method === 'tools/list') {
      if (message.params?.cursor) { reject(message.id, -32602, '没有更多工具分页'); return }
      respond(message.id, { tools: listed.map(tool => ({ name: tool.name, description: tool.description, inputSchema: tool.input_schema })) })
      return
    }
    if (message.method !== 'tools/call') {
      reject(message.id, -32601, '此 MCP 桥仅提供工具接口，不支持该方法')
      return
    }
    const params = message.params
    if (!object(params) || !object(params.arguments)
        || !(allowed.includes(params.name) || (extension && params.name === queryTool))) {
      reject(message.id, -32602, '必须提供一个原始工具名称及其参数对象')
      return
    }
    if (active.has(message.id) || active.size >= 16) {
      reject(message.id, -32600, '请求重复或正在执行的请求数超过限制')
      return
    }
    const controller = new AbortController()
    active.set(message.id, controller)
    let dispatched = false
    try {
      // 写入前数量规则：超量时在转发 HTTP 之前拒绝，并保留可信判定记录。
      const quantity = params.arguments.quantity
      if (extension && params.name === 'write' && Number.isInteger(quantity) && quantity > extension.quantity_limit) {
        const callId = `guard-${message.id}`
        const decidedAt = new Date().toISOString()
        log('business_rule_denied', {
          scope: extension.scope, call_id: callId, tool: 'write', phase: 'guard',
          arguments: params.arguments, status: 403, reason: 'quantity_limit_exceeded',
          approval_required: false, http_forwarded: false,
          quantity, quantity_limit: extension.quantity_limit, decision: 'deny', layer: 'candidate_execution',
        })
        respond(message.id, { content: [{ type: 'text', text: JSON.stringify({
          error: '业务写入数量超过执行规则的限制', reason: 'quantity_limit_exceeded',
          approval_required: false, quantity, quantity_limit: extension.quantity_limit,
        }) }], isError: true })
        log('http_tool_finish', { rpc_id: message.id, tool: 'write', status: 403, payload: { reason: 'quantity_limit_exceeded' }, at: decidedAt })
        return
      }
      // 业务扩展的查询工具只转发一次真实 receipt 调用。
      if (extension && params.name === queryTool) {
        params.forwarded_name = 'receipt'
      }
      const timeout = remaining()
      log('http_tool_start', { rpc_id: message.id, tool: params.name, arguments: params.arguments })
      dispatched = true
      const response = await fetch(binding.tools_url + '/call', {
        method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: params.forwarded_name ?? params.name, arguments: params.arguments }),
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(timeout)]),
      })
      const text = await response.text()
      if (text.length > 2 * 1024 * 1024) throw new Error('HTTP 工具响应大小超过限制；副作用状态未知')
      const payload = JSON.parse(text)
      log('http_tool_finish', { rpc_id: message.id, tool: params.name, status: response.status, payload })
      respond(message.id, { content: [{ type: 'text', text: JSON.stringify({ status: response.status, body: payload }) }], isError: !response.ok })
    } catch (error) {
      const messageText = redact(error instanceof Error ? error.message : error)
      log('http_tool_error', { rpc_id: message.id, tool: params.name, dispatched, side_effect_state: dispatched ? 'unknown' : 'not_dispatched', error: messageText })
      respond(message.id, { content: [{ type: 'text', text: JSON.stringify({ error: messageText, side_effect_state: dispatched ? 'unknown' : 'not_dispatched', automatically_retried: false }) }], isError: true })
    } finally { active.delete(message.id) }
  }

  const input = createInterface({ input: process.stdin, crlfDelay: Infinity, terminal: false })
  const jobs = new Set()
  const stop = reason => {
    input.close()
    process.stdin.destroy()
    for (const controller of active.values()) controller.abort(reason)
  }
  process.once('SIGTERM', () => stop(new Error('MCP 桥收到 SIGTERM')))
  process.once('SIGINT', () => stop(new Error('MCP 桥收到 SIGINT')))
  for await (const line of input) {
    if (line.length > 2 * 1024 * 1024) throw new Error('stdio MCP 消息大小超过限制')
    let message
    try { message = JSON.parse(line) } catch { reject(null, -32700, 'JSON 无效'); continue }
    const job = dispatch(message).catch(error => {
      process.stderr.write(redact(error instanceof Error ? error.message : error) + '\n')
      process.exitCode = 1
      stop(error)
    })
    jobs.add(job)
    void job.finally(() => jobs.delete(job))
  }
  for (const controller of active.values()) controller.abort(new Error('原生 MCP 客户端已关闭 stdio；已发送的写入是否生效仍不确定'))
  await Promise.allSettled(jobs)
}

try { await main() } catch (error) { process.stderr.write(String(error instanceof Error ? error.message : error) + '\n'); process.exitCode = 1 }
