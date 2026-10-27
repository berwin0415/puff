/** 官方 MCP 客户端、原始工具结构和传输证据；不实现页面流程。 */
import { AsyncLocalStorage } from 'node:async_hooks'
import { createRequire } from 'node:module'
import { resolve } from 'node:path'
import { BROWSER_TOOLS, appendEvidence, copyNativeDownload, nativeDownloadPaths } from './detailed-options.mjs'

export async function createBrowserClient({ browser, scope, outputDir, workspace, redact, deadlineAt }, signal) {
  // 解析官方 OpenClaw 已锁定的精确依赖；此辅助模块
  // 在宿主机仅为语法检查加载，不执行 SDK。
  const require = createRequire('/opt/openclaw/package.json')
  const { Client } = require('@modelcontextprotocol/sdk/client/index.js')
  const { StreamableHTTPClientTransport } = require('@modelcontextprotocol/sdk/client/streamableHttp.js')
  const client = new Client({ name: 'harness-lab-openclaw-browser', version: '1.0.0' }, { capabilities: {} })
  const transport = new StreamableHTTPClientTransport(new URL(browser.url))
  const active = new AsyncLocalStorage()
  const requests = new Map()
  const rawPath = resolve(outputDir, 'mcp-events.jsonl')
  const append = (name, value) => appendEvidence(resolve(outputDir, name), { time: new Date().toISOString(), scope, ...value }, redact)
  const wire = (direction, message, metadata) => appendEvidence(rawPath, {
    time: new Date().toISOString(), scope, direction,
    ...(metadata ? { call_id: metadata.callId, native_tool: metadata.nativeName } : {}), message,
  }, redact)
  const send = transport.send.bind(transport)
  transport.send = async (message, options) => {
    for (const original of Array.isArray(message) ? message : [message]) {
      const metadata = original.method === 'tools/call' ? active.getStore() : undefined
      if (metadata && Object.hasOwn(original, 'id')) requests.set(original.id, metadata)
      wire('outbound', original, metadata)
    }
    return await send(message, options)
  }
  // 连接前观测文档所述的公开传输回调，以便同时记录
  // initialize/listTools 以及每个原始响应。
  let onmessage
  Object.defineProperty(transport, 'onmessage', {
    configurable: true,
    get: () => onmessage,
    set: handler => {
      onmessage = typeof handler !== 'function' ? handler : (message, extra) => {
        for (const original of Array.isArray(message) ? message : [message]) {
          const metadata = Object.hasOwn(original, 'id') ? requests.get(original.id) : undefined
          const source = wire('inbound', original, metadata)
          if (metadata && (Object.hasOwn(original, 'result') || Object.hasOwn(original, 'error'))) {
            metadata.responseSource = source
            metadata.response = original
            requests.delete(original.id)
          }
        }
        handler(message, extra)
      }
    },
  })
  const requestOptions = parentSignal => {
    const remaining = Math.min(30_000, deadlineAt - Date.now())
    if (remaining <= 0) throw new Error('浏览器任务已超过执行时限')
    return { timeout: remaining, signal: parentSignal ? AbortSignal.any([parentSignal, AbortSignal.timeout(remaining)]) : AbortSignal.timeout(remaining) }
  }
  const close = async () => {
    let timer
    try {
      await Promise.race([client.close(), new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('MCP 关闭超时')), 2000); timer.unref() })])
    } finally { clearTimeout(timer) }
  }
  try {
    await client.connect(transport, requestOptions(signal))
    const discovered = []
    let cursor
    for (let page = 0; page < 10; page++) {
      const response = await client.listTools(cursor ? { cursor } : {}, requestOptions(signal))
      discovered.push(...response.tools)
      cursor = response.nextCursor
      if (!cursor) break
    }
    if (cursor) throw new Error('MCP 工具元数据超过大小上限')
    const tools = BROWSER_TOOLS.map(name => {
      const matches = discovered.filter(tool => tool.name === name)
      if (matches.length !== 1 || typeof matches[0].description !== 'string' || matches[0].inputSchema?.type !== 'object') throw new Error(`原始浏览器工具缺失或无效：${name}`)
      const tool = matches[0]
      return { name: tool.name, description: tool.description, input_schema: structuredClone(tool.inputSchema) }
    })
    let downloaded = false
    return {
      tools,
      close,
      async call(name, arguments_, callId, callSignal) {
        if (!BROWSER_TOOLS.includes(name) || typeof callId !== 'string' || !callId) throw new Error('已绑定的浏览器工具调用无效')
        const metadata = { callId, nativeName: name }
        let result, failure
        try {
          result = await active.run(metadata, () => client.callTool({ name, arguments: arguments_ }, undefined, requestOptions(callSignal)))
        } catch (error) { failure = error }
        if (metadata.responseSource) {
          const response = metadata.response
          const status = failure || response.error || response.result?.isError ? 500 : 200
          append('browser-actions.jsonl', { tool: name, native_tool: name, phase: 'finish', status, call_id: callId, source: metadata.responseSource })
          if (status === 200) for (const nativePath of nativeDownloadPaths(response.result)) {
            if (downloaded) {
              append('download-events.jsonl', { call_id: callId, source: metadata.responseSource, native_path: nativePath, status: 'additional_download_not_exported' })
              continue
            }
            try {
              const copied = copyNativeDownload(nativePath, browser.artifacts_dir, workspace)
              downloaded = true
              append('download-events.jsonl', { call_id: callId, source: metadata.responseSource, status: 'copied', ...copied })
            } catch (error) {
              append('download-events.jsonl', { call_id: callId, source: metadata.responseSource, native_path: nativePath, status: 'missing', error: error instanceof Error ? error.message : String(error) })
              failure ??= error
            }
          }
        } else {
          append('mcp-client-events.jsonl', { call_id: callId, native_tool: name, phase: 'error', error: failure instanceof Error ? failure.message : '缺少原生 MCP 响应' })
          failure ??= new Error('缺少原生 MCP 响应')
        }
        if (failure) throw failure
        return result
      },
    }
  } catch (error) {
    await close().catch(() => {})
    throw error
  }
}
