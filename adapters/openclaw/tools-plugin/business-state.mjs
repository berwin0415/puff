/** 业务数据保存在公开原生会话记录中，不另建 Agent 或流程循环。 */
import { createHash } from 'node:crypto'
import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { createRequire } from 'node:module'
import { dirname, resolve } from 'node:path'
import { pathToFileURL } from 'node:url'
import { redactor } from './detailed-options.mjs'

export const BUSINESS_PLUGIN = 'harness-lab-tools'
export const BUSINESS_NAMESPACE = 'business'
const scopeFields = ['experiment_id', 'variant', 'user_id', 'session_id']
const operationId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u
const object = value => value !== null && typeof value === 'object' && !Array.isArray(value)
const sameScope = (left, right) => scopeFields.every(key => typeof left?.[key] === 'string' && left[key] === right?.[key])

export function nativeSessionIdentity(request) {
  const sessionValue = 'lab-' + createHash('sha256').update(JSON.stringify([request.run_id, request.session_id])).digest('hex')
  return { agentId: 'lab-a', sessionKey: `agent:lab-a:openai-user:${sessionValue}`, sessionValue }
}

export function readBusinessState(entry, scope, sessionId = entry?.sessionId) {
  const state = entry?.pluginExtensions?.[BUSINESS_PLUGIN]?.[BUSINESS_NAMESPACE]
  if (state === undefined) return null
  if (!object(state) || state.schema_version !== 1 || !sameScope(state.scope, scope) || typeof sessionId !== 'string' || entry?.sessionId !== sessionId || state.native_session_id !== sessionId || !object(state.operations) || Object.keys(state.operations).length > 128 || typeof state.operation_id !== 'string' || !operationId.test(state.operation_id) || typeof state.receipt_confirmed !== 'boolean' || !Object.hasOwn(state.operations, state.operation_id)) throw new Error('原生业务状态身份或结构不匹配')
  for (const [key, value] of Object.entries(state.operations)) {
    if (!operationId.test(key) || !object(value) || value.operation_id !== key || typeof value.receipt_confirmed !== 'boolean' || !Number.isSafeInteger(value.write_count) || value.write_count < 0 || !Array.isArray(value.receipts) || value.write_count !== value.receipts.length || value.receipt_confirmed !== (value.write_count > 0)) throw new Error('原生业务操作记录无效')
    if (value.receipts.some(receipt => !object(receipt) || receipt.operation_id !== key || !sameScope(receipt, scope))) throw new Error('保存的回执不属于对应操作或作用域')
    if (value.receipt_confirmed && (!['receipt', 'query_order_status'].includes(value.receipt_source_tool) || typeof value.receipt_source_call_id !== 'string' || !value.receipt_source_call_id || value.source?.path !== 'http-tool-events.jsonl' || !Number.isSafeInteger(value.source.line) || value.source.line < 1)) throw new Error('已确认的原生操作缺少回执 HTTP 完成来源')
  }
  if (state.receipt_confirmed !== state.operations[state.operation_id].receipt_confirmed) throw new Error('当前原生回执确认状态与其操作不匹配')
  return structuredClone(state)
}

/** 保留实际公开原生记录，并向验收器提供其状态字段位置。 */
function exportBusinessState(entry, scope, outputDir, filename, identity, redact = redactor([])) {
  const state = readBusinessState(entry, scope)
  if (!state) throw new Error('原生业务状态不存在，无法导出')
  const nativeEntry = { sessionId: entry.sessionId, pluginExtensions: { [BUSINESS_PLUGIN]: { [BUSINESS_NAMESPACE]: entry.pluginExtensions[BUSINESS_PLUGIN][BUSINESS_NAMESPACE] } } }
  const raw = redact.json({ native_api: 'openclaw/plugin-sdk/session-store-runtime', agent_id: identity.agentId, session_key: identity.sessionKey, selection: 'exact_plugin_owned_fields_from_public_native_row;not_full_session_entry', native_entry: nativeEntry }) + '\n'
  const sha256 = createHash('sha256').update(raw).digest('hex')
  const sourcePath = 'native-business-state-' + sha256 + '.json'
  const sourceFile = resolve(outputDir, sourcePath)
  if (!existsSync(sourceFile)) writeFileSync(sourceFile, raw, { mode: 0o600, flag: 'wx' })
  else if (readFileSync(sourceFile, 'utf8') !== raw) throw new Error('原生状态快照内容发生变化')
  const projection = { scope, business_state: state, source: { path: sourcePath, sha256, json_pointer: `/native_entry/pluginExtensions/${BUSINESS_PLUGIN}/${BUSINESS_NAMESPACE}` } }
  writeFileSync(resolve(outputDir, filename), redact.json(projection) + '\n', { mode: 0o600 })
  return projection
}

function confirmedReceipt(toolName, operation, callId, event, source, scope) {
  if (!['receipt', 'query_order_status'].includes(toolName)) return null
  if (source?.path !== 'http-tool-events.jsonl' || !Number.isSafeInteger(source.line) || source.line < 1 || event?.phase !== 'finish' || !sameScope(event.scope, scope) || event.tool !== 'receipt' || event.exposed_tool !== toolName || event.call_id !== String(callId) || !event.call_id || event.arguments?.operation_id !== operation || event.status !== 200) throw new Error('回执确认必须来自实际且同作用域的 HTTP 完成记录')
  const result = event.result?.result
  const receipts = result?.operation_id === operation && Number.isSafeInteger(result.write_count) && result.write_count > 0 && Array.isArray(result.receipts) && result.write_count === result.receipts.length ? result.receipts : null
  if (!receipts?.length || receipts.some(receipt => !object(receipt) || receipt.operation_id !== operation || !sameScope(receipt, scope))) return null
  return { receipts: structuredClone(receipts), source: structuredClone(source) }
}

/** 仅使用已准入插件获得的官方运行时接口。 */
export function createBusinessState(api, binding, context, record, redact) {
  const native = api.runtime.agent.session
  if (typeof native?.getSessionEntryAsync !== 'function' || typeof native.prepareSessionEntryPatch !== 'function' || typeof context.assertInvocationCurrent !== 'function') throw new Error('必须提供原生会话状态 API 和 V2 调用授权')
  const target = { agentId: context.agentId, sessionKey: context.sessionKey, env: process.env }
  const assertCurrent = () => context.assertInvocationCurrent()
  let loaded = false
  const read = async () => {
    assertCurrent()
    const entry = await native.getSessionEntryAsync(target)
    assertCurrent()
    if (!entry || entry.sessionId !== context.sessionId) throw new Error('读取业务状态期间，原生会话实例发生变化')
    const state = readBusinessState(entry, binding.scope, context.sessionId)
    if (!loaded) {
      record('native_business_state_loaded', { state, native_api: 'runtime.agent.session.getSessionEntryAsync', task_resume_claim: false }, { agentId: context.agentId, sessionKey: context.sessionKey, sessionId: context.sessionId })
      loaded = true
    }
    return state
  }
  const patch = async (operation, toolName, callId, confirmation) => {
    if (typeof operation !== 'string' || !operationId.test(operation)) throw new Error('业务 operation_id 无效')
    assertCurrent()
    const entry = await native.prepareSessionEntryPatch({
      ...target, preserveActivity: true, requireWriteSuccess: true,
      authority: { kind: 'host', assertCurrent },
      prepare(existing) {
        if (existing.sessionId !== context.sessionId) throw new Error('业务状态提交前，原生会话实例发生变化')
        const current = readBusinessState(existing, binding.scope, context.sessionId)
        const operations = { ...(current?.operations ?? {}) }
        if (!Object.hasOwn(operations, operation) && Object.keys(operations).length >= 128) throw new Error('业务状态操作数量已达上限；未淘汰任何记录')
        const previous = Object.hasOwn(operations, operation) ? operations[operation] : null
        const confirmed = confirmation?.receipts ?? previous?.receipts ?? []
        operations[operation] = { operation_id: operation, receipt_confirmed: confirmed.length > 0, write_count: confirmed.length, receipts: confirmed, last_tool: toolName, last_call_id: String(callId), receipt_source_tool: confirmation !== null ? toolName : previous?.receipt_source_tool ?? null, receipt_source_call_id: confirmation !== null ? String(callId) : previous?.receipt_source_call_id ?? null, source: confirmation?.source ?? previous?.source ?? null, updated_at: new Date().toISOString() }
        const state = { schema_version: 1, scope: binding.scope, native_session_id: context.sessionId, operation_id: operation, receipt_confirmed: operations[operation].receipt_confirmed, operations }
        return { pluginExtensions: { ...existing.pluginExtensions, [BUSINESS_PLUGIN]: { ...existing.pluginExtensions?.[BUSINESS_PLUGIN], [BUSINESS_NAMESPACE]: state } } }
      },
    })
    if (!entry) throw new Error('提交原生业务状态时未找到对应会话')
    const state = readBusinessState(entry, binding.scope, context.sessionId)
    exportBusinessState(entry, binding.scope, dirname(binding.events_path), 'business-state.json', context, redact)
    record('native_business_state_committed', { operation_id: operation, receipt_confirmed: state.operations[operation].receipt_confirmed, state, native_api: 'runtime.agent.session.prepareSessionEntryPatch', external_write_atomic_with_state: false }, { agentId: context.agentId, sessionKey: context.sessionKey, sessionId: context.sessionId })
  }
  return {
    async before(toolName, arguments_, callId) {
      await read()
      if (['write', 'receipt', 'query_order_status'].includes(toolName)) await patch(arguments_.operation_id, toolName, callId, null)
      assertCurrent()
    },
    async after(toolName, arguments_, callId, event, source) {
      if (!['receipt', 'query_order_status'].includes(toolName)) return
      const confirmation = confirmedReceipt(toolName, arguments_.operation_id, callId, event, source, binding.scope)
      if (confirmation !== null) await patch(arguments_.operation_id, toolName, callId, confirmation)
    },
  }
}

/** 检查器仅导入一个公开 SDK 子路径，不启动 Gateway 或 Agent。 */
export async function inspectBusinessState(request, scope) {
  const require = createRequire('/opt/openclaw/package.json')
  const { getSessionEntryAsync } = await import(pathToFileURL(require.resolve('openclaw/plugin-sdk/session-store-runtime')).href)
  const identity = nativeSessionIdentity(request)
  const entry = await getSessionEntryAsync({ agentId: identity.agentId, sessionKey: identity.sessionKey, env: process.env, readConsistency: 'latest' })
  if (!entry) throw new Error('原始原生会话不存在；检查不会创建替代会话')
  const state = readBusinessState(entry, scope)
  if (state === null) throw new Error('原生会话没有业务扩展状态')
  const projection = exportBusinessState(entry, scope, request.output_dir, 'business-state-reconnected.json', identity)
  return { status: 'inspected', stop_reason: 'native_business_state_read_only', native_api: 'openclaw/plugin-sdk/session-store-runtime.getSessionEntryAsync', agent_id: identity.agentId, session_key: identity.sessionKey, native_session_id: entry.sessionId, ...projection, task_resume_claim: false, prompt_submitted: false, gateway_started: false, model_called_by_entry: false, business_tools_called_by_entry: false }
}
