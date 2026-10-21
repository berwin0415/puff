/** Independent I22 image command. The ordinary detailed entry is unchanged. */
import { readFileSync } from 'node:fs'
import { checkedExistingPath, readTrustedJson, readTrustedScope } from './detailed-options.mjs'
import { readGoalBinding, validateGoalRequest } from './goal-binding.mjs'
import { verifySource } from './verify-source.mjs'
import { GoalRuntime } from './goal-runtime.ts'
import type { RunRequest } from './entrypoint.ts'

async function main() {
  if (process.argv.length !== 4 || process.argv[2] !== '--request') throw new Error('Usage: goal-runtime-entrypoint --request /config/request.json')
  const provenance = verifySource()
  const request = validateGoalRequest(readTrustedJson(process.argv[3])) as RunRequest
  const scope = readTrustedScope(request.candidate_options!.trusted_scope_file, request.session_id)
  const trusted = readGoalBinding(request, provenance, scope)
  if (process.env.MODEL_API_BASE_URL !== 'http://model-proxy:8082/v1' || !process.env.MODEL_NAME) throw new Error('Goal run requires the shared counting proxy/model')
  if (process.env.MODEL_RUN_TOKEN_FILE !== '/budget/run.token') throw new Error('Goal run must reuse the original private model run token mount')
  const token = readFileSync(checkedExistingPath(process.env.MODEL_RUN_TOKEN_FILE), 'utf8').trim()
  if (!token || token.length > 8192 || /\s/u.test(token)) throw new Error('Invalid shared local run token')
  if (process.env.MODEL_API_KEY && process.env.MODEL_API_KEY !== token) throw new Error('Model token override does not match the shared chain token')
  trusted.verifyToken(token)
  process.env.MODEL_API_KEY = token
  const { runScopedRuntime } = await import('./scoped-runtime-entrypoint.ts')
  await runScopedRuntime(request, provenance, new GoalRuntime(request, trusted))
}

main().catch(error => {
  // Do not echo arbitrary configuration/SDK errors containing secret text.
  const message = error instanceof Error ? error.message : String(error)
  process.stderr.write(message.replaceAll(process.env.MODEL_API_KEY ?? '\u0000', '[redacted-run-token]') + '\n')
  process.exitCode = 1
})
