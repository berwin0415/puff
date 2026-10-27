/** 独立 I23 故障镜像命令；普通详细验证入口保持原样。 */
import { readFileSync } from 'node:fs'
import { checkedExistingPath, readTrustedJson, readTrustedScope } from './detailed-options.mjs'
import { readFaultGoalBinding, validateFaultGoalRequest } from './fault-goal-binding.mjs'
import { verifySource } from './verify-source.mjs'
import { FaultGoalRuntime, type FaultRunRequest } from './fault-goal-runtime.ts'

async function main() {
  if (process.argv.length !== 4 || process.argv[2] !== '--request') throw new Error('用法：fault-goal-entrypoint --request /config/request.json')
  const provenance = verifySource()
  const request = validateFaultGoalRequest(readTrustedJson(process.argv[3])) as FaultRunRequest
  const scope = readTrustedScope(request.candidate_options!.trusted_scope_file, request.session_id)
  const trusted = readFaultGoalBinding(request, provenance, scope)
  if (process.env.MODEL_API_BASE_URL !== 'http://model-proxy:8082/v1' || !process.env.MODEL_NAME) throw new Error('Goal 运行必须使用共享计量代理和模型')
  if (process.env.MODEL_RUN_TOKEN_FILE !== '/budget/run.token') throw new Error('Goal 运行必须复用原始私有模型运行令牌挂载')
  const token = readFileSync(checkedExistingPath(process.env.MODEL_RUN_TOKEN_FILE), 'utf8').trim()
  if (!token || token.length > 8192 || /\s/u.test(token)) throw new Error('共享本地运行令牌无效')
  if (process.env.MODEL_API_KEY && process.env.MODEL_API_KEY !== token) throw new Error('覆盖的模型令牌与共享执行链令牌不匹配')
  trusted.verifyToken(token)
  process.env.MODEL_API_KEY = token
  const { runScopedRuntime } = await import('./fault-scoped-runtime-entrypoint.ts')
  await runScopedRuntime(request, provenance, new FaultGoalRuntime(request, trusted))
}

main().catch(error => {
  // 不回显可能包含敏感文本的任意配置或 SDK 错误。
  const message = error instanceof Error ? error.message : String(error)
  process.stderr.write(message.replaceAll(process.env.MODEL_API_KEY ?? '\u0000', '[redacted-run-token]') + '\n')
  process.exitCode = 1
})
