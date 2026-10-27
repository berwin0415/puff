/** 仅用于 P05 草案校准；不提供 Goal 或提示词，禁止分发模型请求。 */
import { mkdtempSync, writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { canonicalJson, sha256 } from './goal-binding.mjs'
import { checkedExistingPath, readTrustedJson, readTrustedScope } from './detailed-options.mjs'
import { mountHttpTools } from './tools-plugin.mjs'
import { verifySource } from './verify-source.mjs'

async function main() {
  if (process.argv.length !== 4 || process.argv[2] !== '--request') throw new Error('用法：goal-surface-entrypoint --request /config/surface.json')
  const provenance = verifySource()
  const request = readTrustedJson(process.argv[3])
  const keys = ['session_id', 'workspace', 'output_dir', 'tools_url', 'tool_token_file', 'trusted_scope_file', 'max_output_tokens', 'max_retries']
  if (!request || typeof request !== 'object' || Array.isArray(request) || keys.some(key => !Object.hasOwn(request, key)) || Object.keys(request).some(key => !keys.includes(key))) throw new Error('接口校准请求无效')
  const scope = readTrustedScope(request.trusted_scope_file, request.session_id)
  if (request.tools_url !== 'http://tools:8081' || request.tool_token_file !== '/config/scope.token' || request.trusted_scope_file !== '/config/scope.json' || !Number.isSafeInteger(request.max_output_tokens) || !Number.isSafeInteger(request.max_retries)) throw new Error('校准工具或配置不符合预期')
  for (const key of ['workspace', 'output_dir', 'tool_token_file']) checkedExistingPath(request[key])
  const home = mkdtempSync('/tmp/harness-lab-goal-surface-')
  process.env.DSH_HOME = home
  process.env.HARNESS_GOAL_SESSION_ROOT = resolve(home, 'sessions')
  process.env.HARNESS_TOOLS_URL = request.tools_url
  process.env.HARNESS_TOOL_TOKEN_FILE = request.tool_token_file
  process.env.HARNESS_MAX_OUTPUT_TOKENS = String(request.max_output_tokens)
  process.env.HARNESS_MODEL_MAX_RETRIES = String(request.max_retries)
  process.env.DSH_TELEMETRY_DISABLED = '1'
  process.env.DSH_TOOLS_MODE = 'native'
  // 使用无效占位令牌；此校准不会接收真实模型运行令牌。
  process.env.MODEL_API_KEY = 'surface-calibration-no-model-authorization'
  process.chdir(request.workspace)
  const { initProfile } = await import('../sources/main/packages/boot/app-boot/src/profile.ts')
  initProfile(resolve(home, 'profiles', 'harness-lab-goal'), ['@deepseek-ai/dsh-sdk-minimal'], 'startup')
  const { runProfile } = await import('../sources/main/apps/cli/src/profile-boot.ts')
  const { createLaunchEnvironmentSnapshot } = await import('../sources/main/packages/util/launch-environment/src/index.ts')
  const runtime = await runProfile({ profile: 'harness-lab-goal', args: [],
    environment: createLaunchEnvironmentSnapshot([{ source: 'process', values: process.env as Record<string, string> }]),
    patchFiles: ['cordis.patch.yml', 'scoped-runtime.cordis.patch.yml', 'goal-runtime.cordis.patch.yml'].map(name => resolve(import.meta.dirname, name)) })
  const { SessionId } = await import('../sources/main/packages/core/session/src/types.ts')
  let handle
  let attempts = 0
  runtime.ctx.on('llm/stream', () => { attempts++; throw new Error('冻结接口校准期间禁止分发模型请求') }, { prepend: true })
  try {
    handle = await runtime.ctx.agents.create({ sessionId: SessionId(request.session_id), meta: { cwd: request.workspace },
      agentOptions: { provider: 'deepseek-official', model: process.env.MODEL_NAME!, maxTokens: request.max_output_tokens },
      setup: async agentCtx => {
        agentCtx.tools.guard(exec => exec.name === 'sensitive_export' ? 'sensitive_operation_denied' : undefined)
        await agentCtx.plugin({ name: 'harness-lab-scoped-http-tools', inject: ['tools'], apply: mountHttpTools })
      } })
    const schemas = handle.agent.ctx.tools.schemas(handle.agent).sort((a, b) => a.name.localeCompare(b.name, 'en'))
    if (attempts !== 0 || runtime.ctx.goals.get(handle.agent) || handle.agent.status !== 'idle' || handle.agent.inbox.hasPending) throw new Error('接口校准意外触发了候选工作')
    writeFileSync(resolve(request.output_dir, 'goal-tool-surface.json'), canonicalJson({ schema: 'harness-lab-goal-tool-surface/1', provenance, scope,
      declared_image_id: process.env.HARNESS_LIFECYCLE_IMAGE_ID, schemas, contract_sha256: sha256(canonicalJson(schemas)), model_dispatch_attempts: attempts,
      acceptance: 'host_must_pin_actual_image_id_and_independently_accept_calibration_before_issuing_lifecycle_binding' }) + '\n', { flag: 'wx' })
  } finally {
    await handle?.dispose()
    await runtime.shutdown.shutdown(0)
  }
}
main().catch(error => { process.stderr.write((error instanceof Error ? error.message : String(error)) + '\n'); process.exitCode = 1 })
