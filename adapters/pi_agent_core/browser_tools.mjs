/** 使用 Pi 官方 pi-mcp 客户端接入浏览器基础操作。 */
import { createHash } from 'node:crypto'
import { randomUUID } from 'node:crypto'
import { BROWSER_TOOLS, REQUIRED_BROWSER_TOOLS, copyDownload, nativeDownloadPaths } from './detailed_support.mjs'

/** MCP 工具结果转成 Pi 的 content 结构，保留原始文本块。 */
function toContent(result) {
  const blocks = []
  for (const block of result?.content ?? []) {
    if (block?.type === 'text') blocks.push({ type: 'text', text: block.text ?? '' })
    else blocks.push({ type: 'text', text: JSON.stringify(block) })
  }
  if (blocks.length === 0) blocks.push({ type: 'text', text: '' })
  return blocks
}

export async function openBrowserTools(options, workspace, evidence, timeoutSeconds, mcp) {
  const timeout = Math.min(60, timeoutSeconds)
  const transport = new mcp.StreamableHttpTransport({ url: options.url })
  const client = new mcp.McpClient({ name: 'harness-lab-pi', version: '0.1.0' })
  const initialized = await client.connect(transport)
  evidence.append('mcp-events.jsonl', {
    time: evidence.now(), scope: evidence.scope, phase: 'initialize', result: initialized ?? null,
  })
  // 官方签名：listTools(options) 直接返回 Tool[]。
  const tools = await client.listTools()
  evidence.append('mcp-events.jsonl', {
    time: evidence.now(), scope: evidence.scope, phase: 'list_tools',
    result: { tools: tools.map(tool => ({ name: tool.name, description: tool.description, inputSchema: tool.inputSchema })) },
  })
  const definitions = new Map()
  for (const tool of tools) {
    if (!tool?.name) continue
    if (definitions.has(tool.name)) throw new Error('浏览器 MCP 返回了重复的工具')
    definitions.set(tool.name, tool)
  }
  if (REQUIRED_BROWSER_TOOLS.some(name => !definitions.has(name))) throw new Error('浏览器 MCP 缺少必需的基础操作工具')
  const allowed = [...definitions.keys()].filter(name => BROWSER_TOOLS.has(name)).sort()
  evidence.save('browser-tool-surface.json', {
    scope: evidence.scope, transport: 'official_pi_mcp_streamable_http', sdk: '@earendil-works/pi-mcp@1.3.0',
    discovered: [...definitions.keys()].sort(), allowed,
    business_http_tools: false, script_tools: false,
  })
  const state = { download: null }
  const adapters = allowed.map(name => {
    const definition = definitions.get(name)
    return {
      name, label: name, description: definition.description ?? name,
      parameters: definition.inputSchema ?? { type: 'object', properties: {} },
      async execute(_toolCallId, arguments_, _signal) {
        const callId = randomUUID().replaceAll('-', '')
        const base = { scope: evidence.scope, call_id: callId, native_tool: name }
        evidence.append('mcp-events.jsonl', { ...base, time: evidence.now(), phase: 'start', arguments: arguments_ })
        // 官方签名：callTool(name, args?, options?)。
        const result = await client.callTool(name, arguments_, { timeoutMs: timeout * 1000 })
        const isError = Boolean(result?.isError)
        const status = isError ? 500 : 200
        const source = evidence.append('mcp-events.jsonl', {
          ...base, time: evidence.now(), phase: 'finish', status, result,
        })
        evidence.append('browser-actions.jsonl', { ...base, tool: name, phase: 'finish', status, source })
        if (!isError) {
          for (const nativePath of nativeDownloadPaths(result)) {
            try {
              if (state.download) {
                evidence.append('download-events.jsonl', { ...base, source, native_path: nativePath, status: 'additional_download_not_exported' })
                continue
              }
              state.download = copyDownload(nativePath, options.artifacts_dir, workspace)
              evidence.append('download-events.jsonl', { ...base, source, status: 'copied', ...state.download })
            } catch (error) {
              evidence.append('download-events.jsonl', { ...base, source, native_path: nativePath, status: 'missing', error: String(error?.message ?? error) })
            }
          }
        }
        return { content: toContent(result), details: result, isError }
      },
    }
  })
  return { tools: adapters, close: () => client.close?.() ?? Promise.resolve() }
}
