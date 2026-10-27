/** 仅用于 I23 原始 HTTP 调用栈内的报告；不保存、重试或恢复。 */
import { lstat, readFile } from 'node:fs/promises'

const scopeFields = ['experiment_id', 'session_id', 'user_id', 'variant']
const fields = ['attempt_id', 'chain_id', 'schema', 'scope', 'token_file']
const tokenHeader = 'X-Harness-Prewrite-Token'

async function bounded(path, maximum) {
  if (!path.startsWith('/config/') || path.includes('..')) throw new Error('必须使用固定的 I23 凭证路径')
  const info = await lstat(path)
  if (!info.isFile() || info.isSymbolicLink() || info.size > maximum) throw new Error('I23 凭证文件无效')
  const data = await readFile(path, 'utf8')
  if (Buffer.byteLength(data) > maximum) throw new Error('I23 凭证超过大小上限')
  return data
}

export async function loadI23Prewrite(endpoint, scopeAuthorization) {
  const path = process.env.HARNESS_I23_PREWRITE_BINDING_FILE
  if (path === undefined) return undefined
  if (path !== '/config/prewrite.json' || endpoint.href !== 'http://tools:8081/') throw new Error('必须使用固定的 I23 接口和绑定')
  const binding = JSON.parse(await bounded(path, 65536))
  const trustedScope = JSON.parse(await bounded('/config/scope.json', 65536))
  if (!binding || JSON.stringify(Object.keys(binding).sort()) !== JSON.stringify(fields)
      || binding.schema !== 'harness-lab-prewrite-client/1'
      || !trustedScope || JSON.stringify(Object.keys(trustedScope).sort()) !== JSON.stringify(scopeFields)
      || !binding.scope || scopeFields.some(key => binding.scope[key] !== trustedScope[key])
      || JSON.stringify(Object.keys(binding.scope).sort()) !== JSON.stringify(scopeFields)
      || trustedScope.experiment_id !== 'fault-recovery' || binding.chain_id !== trustedScope.variant
      || !['worker-a', 'worker-b'].includes(binding.attempt_id) || binding.token_file !== '/config/prewrite.token') {
    throw new Error('绑定到本次尝试的 I23 写入前配置无效')
  }
  const token = (await bounded(binding.token_file, 8192)).trim()
  if (token.length < 16 || token.length > 512 || /\s/u.test(token)) throw new Error('I23 尝试凭证无效')
  // 此处没有目标、管理接口、预期答案或放行能力。
  return {
    async before(tool, arguments_, nativeCallId, signal) {
      const headers = { [tokenHeader]: token }
      if (tool !== 'write') return headers
      if (typeof nativeCallId !== 'string' || !nativeCallId.length) throw new Error('I23 必须使用实际 execution.callId')
      const report = { chain_id: binding.chain_id, scope: trustedScope, attempt_id: binding.attempt_id,
        bridge_call_id: nativeCallId, native_call_id: nativeCallId, tool, arguments: arguments_ }
      // 这次额外 HTTP 交互仅校准故障窗口，不能
      // 查询回执、刷盘原生状态、调用其他工具或继续任务。
      const response = await fetch(new URL('/experiment/prewrite', endpoint), {
        method: 'POST', headers: { ...headers, Authorization: scopeAuthorization, 'Content-Type': 'application/json' },
        body: JSON.stringify(report), signal,
      })
      if (response.status !== 200) throw new Error('实验写入前报告被拒绝; business HTTP not sent by this bridge')
      const value = await response.json()
      if (!value || JSON.stringify(Object.keys(value)) !== JSON.stringify(['permit'])
          || typeof value.permit !== 'string' || value.permit.length < 16 || value.permit.length > 512 || /\s/u.test(value.permit)) {
        throw new Error('实验写入前许可无效; business HTTP not sent by this bridge')
      }
      return { ...headers, 'X-Harness-Prewrite-Call-Id': nativeCallId, 'X-Harness-Prewrite-Permit': value.permit }
    },
  }
}
