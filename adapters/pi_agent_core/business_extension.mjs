/** Pi 侧业务扩展：回执查询工具与写入前数量规则。 */
import { createHash } from 'node:crypto'

export const QUERY_TOOL = 'query_order_status'

/** 包装原有 HTTP 工具：write 在执行前检查数量上限。 */
export function guardQuantity(tool, quantityLimit, evidence, scope) {
  if (tool.name !== 'write') return tool
  return {
    ...tool,
    description: `${tool.description}（执行前校验写入数量上限）`,
    async execute(toolCallId, arguments_, signal) {
      const quantity = arguments_?.quantity
      if (Number.isInteger(quantity) && quantity > quantityLimit) {
        const callId = toolCallId ?? `guard-${Date.now()}`
        const record = {
          scope, call_id: callId, tool: 'write', phase: 'guard', arguments: arguments_,
          status: 403, reason: 'quantity_limit_exceeded', approval_required: false,
          http_forwarded: false, quantity, quantity_limit: quantityLimit,
        }
        const source = evidence.append('business-tool-events.jsonl', record)
        evidence.append('business-rule-decisions.jsonl', {
          scope, call_id: callId, tool: 'write', decision: 'deny',
          reason: 'quantity_limit_exceeded', approval_required: false,
          http_forwarded: false, layer: 'candidate_execution',
          operation_id: arguments_?.operation_id,
          quantity, quantity_limit: quantityLimit, source,
        })
        return {
          content: [{ type: 'text', text: JSON.stringify({
            error: { code: 'quantity_limit_exceeded', message: '业务写入数量超过执行规则的限制', approval_required: false, quantity, quantity_limit: quantityLimit },
          }) }],
          isError: true,
        }
      }
      // 限额内的写入照常执行：不能靠“全部拒绝”通过验收。
      return tool.execute(toolCallId, arguments_, signal)
    },
  }
}

/** 新增一次性回执查询工具；只转发一次真实请求，不代办后续步骤。 */
export function queryTool(receiptSchema, client, headers, evidence, scope, observations, deadlineSignal) {
  return {
    name: QUERY_TOOL,
    label: QUERY_TOOL,
    description: '查询指定操作编号的真实回执与实际写入次数。只发起一次查询，不代办后续步骤。',
    parameters: receiptSchema,
    async execute(toolCallId, arguments_, signal) {
      const callId = toolCallId ?? `query-${Date.now()}`
      const record = { scope, call_id: callId, tool: 'receipt', exposed_tool: QUERY_TOOL, arguments: arguments_ }
      evidence.append('http-tool-events.jsonl', { ...record, phase: 'start' })
      let response
      try {
        response = await fetch(new URL('/call', client), {
          method: 'POST',
          headers,
          body: JSON.stringify({ name: 'receipt', arguments: arguments_ }),
          signal: signal ? AbortSignal.any([deadlineSignal, signal]) : deadlineSignal,
        })
      } catch (error) {
        evidence.append('http-tool-events.jsonl', { ...record, phase: 'finish', status: 0, error: String(error?.message ?? error) })
        return { content: [{ type: 'text', text: JSON.stringify({ error: { code: 'http_transport_failure' } }) }], isError: true }
      }
      const body = await response.json()
      const source = evidence.append('http-tool-events.jsonl', { ...record, phase: 'finish', status: response.status, result: body })
      if (response.ok) observations.push({ call_id: callId, operation: arguments_?.operation_id, body, source })
      return { content: [{ type: 'text', text: JSON.stringify(body) }], details: body, isError: !response.ok }
    },
  }
}

/** 写业务状态投影：来源必须是留存的原生快照字节。 */
export function writeProjection(outputDir, snapshotPath, bodyState, scope, writeFileSync, readFileSync) {
  const data = readFileSync(snapshotPath)
  const source = {
    path: 'state-snapshot.json',
    sha256: createHash('sha256').update(data).digest('hex'),
    json_pointer: '/harness_lab_business_state',
  }
  writeFileSync(`${outputDir}/business-state.json`, JSON.stringify({ scope, source, business_state: bodyState }, null, 2) + '\n')
}
