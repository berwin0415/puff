/** Minimal stdio MCP transport for the eight original scoped HTTP tools.
 * No Agent, model, business workflow, calculations, file writes or HTTP retries.
 */
import { appendFileSync, existsSync, lstatSync, readFileSync, realpathSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { createInterface } from 'node:readline'

const allowed = ['list_items', 'write', 'receipt', 'ledger', 'read_file', 'write_file', 'calculate_totals', 'sensitive_export']
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)

function trustedPath(value) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('Invalid trusted bridge path')
  const path = resolve(value)
  let current = '/'
  for (const part of path.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('Bridge paths must not follow links')
  }
  if (realpathSync(path) !== path) throw new Error('Bridge path resolution changed')
  return path
}

async function main() {
  const args = process.argv.slice(2)
  if (args.length !== 2 || args[0] !== '--binding') throw new Error('Expected trusted --binding file')
  const bindingPath = trustedPath(args[1])
  if (!lstatSync(bindingPath).isFile() || lstatSync(bindingPath).size > 262144) throw new Error('Expected a bounded regular bridge binding')
  const binding = JSON.parse(readFileSync(bindingPath, 'utf8'))
  if (!object(binding) || binding.tools_url !== 'http://tools:8081' || !Number.isSafeInteger(binding.deadline_at) || typeof binding.run_id !== 'string' || typeof binding.session_id !== 'string') throw new Error('Invalid trusted bridge binding')
  const tools = binding.tools
  if (!Array.isArray(tools) || tools.length !== allowed.length || new Set(tools.map(tool => tool.name)).size !== allowed.length || tools.some(tool => !allowed.includes(tool.name) || typeof tool.description !== 'string' || !object(tool.input_schema) || tool.input_schema.type !== 'object' || tool.input_schema.additionalProperties !== false)) throw new Error('Expected the eight original HTTP tool schemas')
  const tokenPath = trustedPath(binding.token_file)
  if (!lstatSync(tokenPath).isFile() || lstatSync(tokenPath).size > 8192) throw new Error('Expected a bounded regular tool token file')
  const token = readFileSync(tokenPath, 'utf8').trim()
  if (!token || /\s/u.test(token)) throw new Error('Invalid trusted tool token')
  const outputDir = trustedPath(binding.output_dir)
  if (!lstatSync(outputDir).isDirectory()) throw new Error('Expected an evidence directory')
  const redact = value => String(value).replaceAll(token, '[redacted-local-token]')
  const log = (type, record) => appendFileSync(resolve(outputDir, 'mcp-bridge-events.jsonl'), redact(JSON.stringify({ time: new Date().toISOString(), run_id: binding.run_id, session_id: binding.session_id, type, ...record })) + '\n', { mode: 0o600 })
  const send = message => {
    // The response itself is unmodified. Only retained audit copies are redacted.
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
    if (value <= 0) throw new Error('Active task deadline reached before HTTP dispatch')
    return value
  }

  async function dispatch(message) {
    if (!object(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string') {
      reject(message?.id ?? null, -32600, 'Invalid JSON-RPC request')
      return
    }
    const hasId = Object.hasOwn(message, 'id')
    log('stdio_inbound', { message })
    if (message.method === 'notifications/cancelled') {
      active.get(message.params?.requestId)?.abort(new Error('Native MCP client cancelled the HTTP call; any dispatched write remains uncertain'))
      return
    }
    if (!hasId) {
      if (message.method === 'notifications/initialized' && initialized) ready = true
      return
    }
    if (!(typeof message.id === 'string' || Number.isSafeInteger(message.id))) {
      reject(null, -32600, 'Invalid JSON-RPC request identifier')
      return
    }
    if (message.method === 'initialize') {
      if (initialized || !object(message.params) || typeof message.params.protocolVersion !== 'string') {
        reject(message.id, -32602, 'Invalid or duplicate initialization')
        return
      }
      initialized = true
      // This server offers only the stable tools subset. It does not negotiate
      // sampling, elicitation, resources, tasks, prompts or workflow extensions.
      respond(message.id, { protocolVersion: '2025-03-26', capabilities: { tools: { listChanged: false } }, serverInfo: { name: 'harness-lab-original-http-tools', version: '1.0.0' } })
      return
    }
    if (!ready) {
      reject(message.id, -32002, 'MCP initialization has not completed')
      return
    }
    if (message.method === 'ping') { respond(message.id, {}); return }
    if (message.method === 'tools/list') {
      if (message.params?.cursor) { reject(message.id, -32602, 'No additional tool pages'); return }
      respond(message.id, { tools: tools.map(tool => ({ name: tool.name, description: tool.description, inputSchema: tool.input_schema })) })
      return
    }
    if (message.method !== 'tools/call') {
      reject(message.id, -32601, 'Method not offered by this tools-only MCP bridge')
      return
    }
    const params = message.params
    if (!object(params) || !allowed.includes(params.name) || !object(params.arguments)) {
      reject(message.id, -32602, 'Expected one original tool name and its argument object')
      return
    }
    if (active.has(message.id) || active.size >= 16) {
      reject(message.id, -32600, 'Duplicate or excessive in-flight request')
      return
    }
    const controller = new AbortController()
    active.set(message.id, controller)
    let dispatched = false
    try {
      const timeout = remaining()
      log('http_tool_start', { rpc_id: message.id, tool: params.name, arguments: params.arguments })
      dispatched = true
      const response = await fetch(binding.tools_url + '/call', {
        method: 'POST', headers: { Authorization: `Bearer ${token}`, 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: params.name, arguments: params.arguments }),
        signal: AbortSignal.any([controller.signal, AbortSignal.timeout(timeout)]),
      })
      const text = await response.text()
      if (text.length > 2 * 1024 * 1024) throw new Error('Unbounded HTTP tool response; side-effect state is unknown')
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
  process.once('SIGTERM', () => stop(new Error('MCP bridge received SIGTERM')))
  process.once('SIGINT', () => stop(new Error('MCP bridge received SIGINT')))
  for await (const line of input) {
    if (line.length > 2 * 1024 * 1024) throw new Error('Unbounded stdio MCP message')
    let message
    try { message = JSON.parse(line) } catch { reject(null, -32700, 'Invalid JSON'); continue }
    const job = dispatch(message).catch(error => {
      process.stderr.write(redact(error instanceof Error ? error.message : error) + '\n')
      process.exitCode = 1
      stop(error)
    })
    jobs.add(job)
    void job.finally(() => jobs.delete(job))
  }
  for (const controller of active.values()) controller.abort(new Error('Native MCP client closed stdio; dispatched writes remain uncertain'))
  await Promise.allSettled(jobs)
}

try { await main() } catch (error) { process.stderr.write(String(error instanceof Error ? error.message : error) + '\n'); process.exitCode = 1 }
