/** Thin container client of the official DSH SDK; it never chooses task steps. */
import { appendFileSync, existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, writeFileSync } from 'node:fs'
import { isAbsolute, resolve } from 'node:path'
import { verifySource } from './verify-source.mjs'
import { validateDetailedOptions } from './detailed-options.mjs'

export type RunRequest = {
  run_id: string
  session_id: string
  prompt: string
  workspace: string
  state_dir: string
  output_dir: string
  tools_url: string
  tool_token_file: string
  max_output_tokens: number
  timeout_seconds: number
  max_retries: number
  candidate_options?: ReturnType<typeof validateDetailedOptions>
  lifecycle?: { mode: 'new' | 'resume'; chain_id: string; attempt_id: 'worker-a' | 'worker-b'; binding_file: '/config/lifecycle.json' }
}

function trustedPath(value: unknown, directory: boolean): string {
  if (typeof value !== 'string' || !isAbsolute(value) || value.split('/').some(part => part === '..' || part.toLowerCase() === 'old')) throw new Error('Invalid trusted run path')
  const absolute = resolve(value)
  let current = '/'
  for (const part of absolute.split('/').filter(Boolean)) {
    current = resolve(current, part)
    if (existsSync(current) && lstatSync(current).isSymbolicLink()) throw new Error('Trusted run paths must not follow symbolic links')
  }
  if (directory) mkdirSync(absolute, { recursive: true })
  if (realpathSync(absolute) !== absolute) throw new Error('Trusted run path changed during resolution')
  return absolute
}

function requestFromFile(): RunRequest {
  if (process.argv.length !== 4 || process.argv[2] !== '--request') throw new Error('Usage: entrypoint --request /config/request.json')
  const value = JSON.parse(readFileSync(trustedPath(process.argv[3], false), 'utf8'))
  const keys = ['run_id', 'session_id', 'prompt', 'workspace', 'state_dir', 'output_dir', 'tools_url', 'tool_token_file', 'max_output_tokens', 'timeout_seconds', 'max_retries']
  if (!value || typeof value !== 'object' || Array.isArray(value) || keys.some(key => !Object.hasOwn(value, key)) || Object.keys(value).some(key => !keys.includes(key) && key !== 'candidate_options')) throw new Error('Invalid trusted run request fields')
  for (const name of ['run_id', 'session_id']) {
    if (typeof value[name] !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u.test(value[name]) || value[name].toLowerCase() === 'old') throw new Error('Invalid run/session identifier')
  }
  if (typeof value.prompt !== 'string' || value.prompt.length === 0 || value.prompt.length > 65536) throw new Error('Expected a bounded task prompt')
  if (!Number.isSafeInteger(value.max_output_tokens) || value.max_output_tokens < 1 || value.max_output_tokens > 16384 || !Number.isSafeInteger(value.timeout_seconds) || value.timeout_seconds < 1 || value.timeout_seconds > 3600 || !Number.isSafeInteger(value.max_retries) || value.max_retries < 0 || value.max_retries > 2) throw new Error('Invalid run budget')
  for (const name of ['workspace', 'state_dir', 'output_dir']) value[name] = trustedPath(value[name], true)
  value.tool_token_file = trustedPath(value.tool_token_file, false)
  if (value.workspace === value.state_dir || value.workspace === value.output_dir || value.state_dir === value.output_dir) throw new Error('State, workspace and evidence must be separate directories')
  if (value.tools_url !== 'http://tools:8081') throw new Error('Expected the scoped experiment tools service')
  if (Object.hasOwn(value, 'candidate_options')) value.candidate_options = validateDetailedOptions(value.candidate_options)
  return value as RunRequest
}

function redact(message: string): string {
  const runToken = process.env.MODEL_API_KEY
  return runToken ? message.replaceAll(runToken, '[redacted-run-token]') : message
}

function loadModelRunToken(): void {
  if (process.env.MODEL_API_KEY) return
  const filename = trustedPath(process.env.MODEL_RUN_TOKEN_FILE, false)
  const stat = lstatSync(filename)
  if (!stat.isFile() || stat.size > 8192) throw new Error('Expected a bounded local model run token file')
  const token = readFileSync(filename, 'utf8').trim()
  if (!token || /\s/u.test(token)) throw new Error('Expected a non-empty local model run token')
  process.env.MODEL_API_KEY = token
}

async function main() {
  const provenance = verifySource()
  const request = requestFromFile()
  loadModelRunToken()
  if (process.env.MODEL_API_BASE_URL !== 'http://model-proxy:8082/v1' || !process.env.MODEL_API_KEY || !process.env.MODEL_NAME) throw new Error('Candidate requires the counting proxy, its local run token and the selected model')
  if (request.candidate_options) {
    const { runScopedRuntime } = await import('./scoped-runtime-entrypoint.ts')
    await runScopedRuntime(request, provenance)
    return
  }
  const { DeepSeekHarness } = await import('../sources/main/packages/sdk/client/src/index.ts')
  process.env.HARNESS_TOOLS_URL = request.tools_url
  process.env.HARNESS_TOOL_TOKEN_FILE = request.tool_token_file
  process.env.HARNESS_MAX_OUTPUT_TOKENS = String(request.max_output_tokens)
  process.env.HARNESS_MODEL_MAX_RETRIES = String(request.max_retries)
  process.env.DSH_TELEMETRY_DISABLED = '1'
  process.env.DSH_TOOLS_MODE = 'native'
  const rawPath = resolve(request.output_dir, 'raw-events.jsonl')
  const started = Date.now()
  let timedOut = false
  let timer: ReturnType<typeof setTimeout> | undefined
  const harness = new DeepSeekHarness({
    profile: 'sdk-minimal',
    patches: [resolve(import.meta.dirname, 'cordis.patch.yml')],
    dshHome: request.state_dir,
    processCwd: request.workspace,
    cwd: request.workspace,
    provider: 'deepseek-official',
    model: process.env.MODEL_NAME,
    maxTokens: request.max_output_tokens,
    initializeTimeoutMs: 30_000,
    requestTimeoutMs: request.timeout_seconds * 1000,
  })
  writeFileSync(resolve(request.output_dir, 'entrypoint.json'), JSON.stringify({
    run_id: request.run_id, session_id: request.session_id, profile: 'sdk-minimal',
    provider: 'deepseek-official', provenance, started_at: new Date(started).toISOString(),
    controls: { pause: 'not_implemented_by_this_entry', resume: 'not_yet_verified', cancel: 'runtime_close_only' },
  }, null, 2) + '\n')
  try {
    const deadline = new Promise<never>((_resolve, reject) => {
      timer = setTimeout(() => { timedOut = true; reject(new Error('Active task deadline reached')) }, request.timeout_seconds * 1000)
    })
    const result = await Promise.race([
      harness.run(request.prompt, {
        sessionId: request.session_id,
        onNotification: notification => appendFileSync(rawPath, JSON.stringify(notification) + '\n'),
      }),
      deadline,
    ])
    const lastTurn = result.events.findLast(event => event.type === 'turn/end')
    const reason = lastTurn?.data?.reason?.kind ?? 'missing_turn_end'
    const summary = { status: reason === 'completed' ? 'completed' : 'incomplete', stop_reason: reason, session_id: result.sessionId, final_response: result.finalResponse, elapsed_seconds: (Date.now() - started) / 1000, native_result: result }
    writeFileSync(resolve(request.output_dir, 'result.json'), JSON.stringify(summary, null, 2) + '\n')
    process.stdout.write(JSON.stringify({ status: summary.status, stop_reason: reason, session_id: request.session_id }) + '\n')
    if (summary.status !== 'completed') process.exitCode = 1
  } catch (error) {
    const summary = { status: timedOut ? 'timed_out' : 'failed', stop_reason: timedOut ? 'active_timeout' : 'runtime_error', error: redact(error instanceof Error ? error.message : String(error)), session_id: request.session_id, elapsed_seconds: (Date.now() - started) / 1000 }
    writeFileSync(resolve(request.output_dir, 'result.json'), JSON.stringify(summary, null, 2) + '\n')
    process.stdout.write(JSON.stringify({ status: summary.status, stop_reason: summary.stop_reason }) + '\n')
    process.exitCode = 1
  } finally {
    if (timer) clearTimeout(timer)
    await harness.close()
  }
}

main().catch(error => {
  process.stderr.write(redact(error instanceof Error ? error.message : String(error)) + '\n')
  process.exitCode = 1
})
