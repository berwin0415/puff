/** 仅校准本地 HTTP 传输，不调用 OpenClaw、模型或业务服务。 */
import assert from 'node:assert/strict'
import { once } from 'node:events'
import { createServer } from 'node:http'
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { pathToFileURL } from 'node:url'
import { test } from 'node:test'

const source = process.env.OPENCLAW_CONTROL_SOURCE
  ? pathToFileURL(process.env.OPENCLAW_CONTROL_SOURCE) : new URL('../../adapters/openclaw/native-control.mjs', import.meta.url)
const { createHttpTaskTransport, isCancelledTerminal } = await import(source.href)
const nativeRunId = 'chatcmpl-local-calibration'

function fixture(t, deadline = () => false) {
  const output = mkdtempSync(join(tmpdir(), 'openclaw-http-control-'))
  t.after(() => rmSync(output, { recursive: true, force: true }))
  return { output, transport: createHttpTaskTransport({ request: { run_id: 'offline-probe', output_dir: output },
    scope: { experiment_id: 'offline', variant: 'offline-probe', user_id: 'alice', session_id: 'main' },
    sessionKey: 'agent:lab-a:offline', encode: JSON.stringify, isDeadlineAborted: deadline }) }
}

test('实际中止 HTTP 读取仅关闭当前客户端，不能证明原生取消成功', { timeout: 5000 }, async t => {
  const { output, transport } = fixture(t)
  let resolveDisconnect
  const closed = new Promise(resolve => { resolveDisconnect = resolve })
  const server = createServer((_request, response) => {
    response.writeHead(200, { 'content-type': 'text/event-stream' })
    response.write('data: ' + JSON.stringify({ id: nativeRunId, choices: [] }) + '\n\n')
    response.once('close', () => resolveDisconnect())
  })
  t.after(() => { server.closeAllConnections(); server.close() })
  server.listen(0, '127.0.0.1')
  await once(server, 'listening')
  transport.start()
  const response = await fetch('http://127.0.0.1:' + server.address().port, { signal: transport.signal })
  transport.receive(response)
  let wire = '', failure
  try {
    for await (const chunk of response.body) {
      wire += new TextDecoder().decode(chunk)
      transport.observeRun(nativeRunId, { path: 'raw-events.jsonl', line: 1 })
      const requested = transport.disconnect({ requestId: 'offline-probe:cancel:1', nativeRunId })
      assert.equal(requested.result.disconnect_requested, true)
      assert.equal(transport.snapshot.settled, undefined)
      assert.equal(requested.result.terminal_confirmation, undefined)
    }
  } catch (error) { failure = error }
  assert.equal(failure?.name, 'AbortError')
  assert.equal(transport.isRequestedAbort(failure), true)
  writeFileSync(join(output, 'http-response.sse'), wire)
  assert.equal(transport.finish({ error: failure, done: false, frameCount: 1, gatewayRunId: nativeRunId }), true)
  assert.equal(transport.snapshot.settled.result.outcome, 'client_abort')
  assert.equal(transport.snapshot.settled.result.terminal_confirmation, false)
  assert.equal(isCancelledTerminal({ runId: nativeRunId, status: 'timeout' }, nativeRunId), false)
  await closed
  assert.equal(readFileSync(join(output, 'http-response.sse'), 'utf8').includes('[DONE]'), false)
})

test('运行不匹配、未提交、重复或已结束的客户端不能安排后续取消', t => {
  const { output, transport } = fixture(t)
  const cancel = () => transport.disconnect({ requestId: 'offline-probe:cancel:1', nativeRunId })
  assert.throws(cancel, /没有可断开的匹配活跃 HTTP/)
  assert.equal(transport.signal.aborted, false)
  transport.start()
  transport.receive({ status: 200, headers: new Headers({ 'content-type': 'text/event-stream' }) })
  transport.observeRun(nativeRunId, { path: 'raw-events.jsonl', line: 1 })
  assert.throws(() => transport.disconnect({ requestId: 'wrong-run', nativeRunId: 'other' }), /没有可断开的匹配活跃 HTTP/)
  assert.equal(transport.signal.aborted, false)
  cancel()
  assert.throws(cancel, /没有可断开的匹配活跃 HTTP/)
  writeFileSync(join(output, 'http-response.sse'), 'partial')
  transport.finish({ error: new DOMException('aborted', 'AbortError'), done: false, frameCount: 1, gatewayRunId: nativeRunId })
  assert.throws(cancel, /没有可断开的匹配活跃 HTTP/)
})

test('超时和无关读取失败不能算作主动取消', t => {
  let deadline = false
  const { output, transport } = fixture(t, () => deadline)
  transport.start()
  transport.receive({ status: 200, headers: new Headers({ 'content-type': 'text/event-stream' }) })
  transport.observeRun(nativeRunId, { path: 'raw-events.jsonl', line: 1 })
  transport.disconnect({ requestId: 'offline-probe:cancel:1', nativeRunId })
  assert.equal(transport.isRequestedAbort(new Error('unrelated')), false)
  deadline = true
  assert.equal(transport.isRequestedAbort(new DOMException('deadline', 'AbortError')), false)
  writeFileSync(join(output, 'http-response.sse'), 'partial')
  transport.finish({ error: new DOMException('deadline', 'AbortError'), done: false, frameCount: 1, gatewayRunId: nativeRunId })
  assert.equal(transport.snapshot.settled.result.outcome, 'read_error')
})

test('HTTP 自然完成或客户端 AbortError 均不能单独证明原生任务结束', t => {
  const { output, transport } = fixture(t)
  transport.start()
  transport.receive({ status: 200, headers: new Headers({ 'content-type': 'text/event-stream' }) })
  transport.observeRun(nativeRunId, { path: 'raw-events.jsonl', line: 1 })
  assert.equal(transport.isRequestedAbort(new DOMException('unrelated', 'AbortError')), false)
  writeFileSync(join(output, 'http-response.sse'), 'data: [DONE]\n\n')
  transport.finish({ done: true, frameCount: 1, gatewayRunId: nativeRunId })
  assert.equal(transport.snapshot.settled.result.outcome, 'complete')
  assert.throws(() => transport.disconnect({ requestId: 'late', nativeRunId }), /没有可断开的匹配活跃 HTTP/)
  const terminal = { runId: nativeRunId, status: 'error', stopReason: 'aborted', endedAt: 1 }
  assert.equal(isCancelledTerminal(terminal, nativeRunId), true)
  for (const changed of [{ status: 'timeout' }, { stopReason: 'rpc' }, { pendingError: true }, { yielded: true }, { endedAt: null }, { runId: 'other' }]) {
    assert.equal(isCancelledTerminal({ ...terminal, ...changed }, nativeRunId), false)
  }
})
