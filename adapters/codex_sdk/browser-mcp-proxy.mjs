/** 把上游 Streamable HTTP MCP 服务原样暴露为 stdio MCP 服务，供 Codex 作为独立 server 使用。
 *
 * 只做协议转发：工具名、描述、inputSchema 和返回内容都来自上游，
 * 不改写、不补默认值、不重试、不代办业务步骤。
 */
import { appendFileSync, existsSync, lstatSync, readFileSync, realpathSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { createInterface } from 'node:readline'

function trustedFile(value) {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('浏览器桥接路径无效')
  const path = resolve(value)
  let current = '/'
  for (const part of path.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('桥接路径不能经过链接')
  }
  return realpathSync(path)
}

const bindingPath = (() => {
  const args = process.argv.slice(2)
  if (args.length !== 2 || args[0] !== '--binding') throw new Error('必须通过 --binding 指定可信文件')
  return trustedFile(args[1])
})()

const binding = JSON.parse(readFileSync(bindingPath, 'utf8'))
if (binding.url !== 'http://browser:8931/mcp' || typeof binding.output_dir !== 'string') throw new Error('可信浏览器桥接绑定无效')
const outputDir = trustedFile(binding.output_dir)
const log = record => appendFileSync(resolve(outputDir, 'browser-mcp-bridge-events.jsonl'),
  JSON.stringify({ time: new Date().toISOString(), type: record.type, ...record }) + '\n', { mode: 0o600 })

const send = message => process.stdout.write(JSON.stringify(message) + '\n')

// 上游会话状态：initialize 后由服务端返回的会话 ID。
let sessionId = null

async function upstream(body) {
  const headers = { 'Content-Type': 'application/json', Accept: 'application/json, text/event-stream' }
  if (sessionId) headers['Mcp-Session-Id'] = sessionId
  const response = await fetch(binding.url, { method: 'POST', headers, body: JSON.stringify(body) })
  const returned = response.headers.get('mcp-session-id')
  if (returned) sessionId = returned
  const text = await response.text()
  const type = response.headers.get('content-type') ?? ''
  if (type.includes('text/event-stream')) {
    // 取第一个 data: 帧作为 JSON-RPC 响应。
    for (const line of text.split('\n')) {
      if (!line.startsWith('data:')) continue
      const payload = line.slice(5).trim()
      if (!payload) continue
      try { return JSON.parse(payload) } catch { /* 继续找下一帧 */ }
    }
    throw new Error('上游返回的 SSE 不含可解析的 JSON-RPC 消息')
  }
  return JSON.parse(text)
}

async function handle(message) {
  if (message.method === undefined && (message.result !== undefined || message.error !== undefined)) return
  // 通知没有 id，上游按 SSE 或空体处理；不要求也不回送响应。
  const isNotification = message.id === undefined
  log({ type: 'request', method: message.method, id: message.id ?? null })
  try {
    const reply = await upstream(message)
    log({ type: 'response', method: message.method, id: message.id ?? null, has_error: Boolean(reply?.error) })
    if (isNotification) return
    send({ jsonrpc: '2.0', id: message.id, result: reply.result, error: reply.error })
  } catch (error) {
    log({ type: 'error', method: message.method, error: String(error?.message ?? error) })
    if (!isNotification) send({ jsonrpc: '2.0', id: message.id, error: { code: -32603, message: String(error?.message ?? error) } })
  }
}

const lines = createInterface({ input: process.stdin })
const pending = []
for await (const line of lines) {
  const text = line.trim()
  if (!text) continue
  let message
  try { message = JSON.parse(text) } catch { continue }
  pending.push(handle(message))
}
await Promise.allSettled(pending)
