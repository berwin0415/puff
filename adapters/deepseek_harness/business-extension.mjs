/** Business observations in public native Session events/projections, not a driver. */
import { createHash } from 'node:crypto'
import { createRequire } from 'node:module'
import { isDeepStrictEqual } from 'node:util'
import { sameScope } from './detailed-options.mjs'

// External required event types are not reloadable in the fixed upstream.
// The real tool scheduler owns this built-in event; never append one here.
export const BUSINESS_EVENT = 'tool/result'
export const BUSINESS_META_KIND = 'harness-lab/business-state/v1'
export const BUSINESS_STATE_POINTER = '/data/meta/business_state'
export const BUSINESS_PROJECTION = 'harness-lab-business'
const operationId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/u

export function emptyBusinessState(sessionId) {
  return { schema_version: 1, session_id: String(sessionId), scope: null,
    operation_id: null, receipt_confirmed: false, operations: {} }
}

export function quantityDenial(arguments_, limit) {
  if (!Number.isSafeInteger(arguments_?.quantity) || arguments_.quantity <= limit) return undefined
  return { reason: 'quantity_limit_exceeded', quantity: arguments_.quantity,
    quantity_limit: limit, operation_id: arguments_.operation_id, http_forwarded: false }
}

function matchingReceipt(receipt, operation, scope) {
  return !!receipt && typeof receipt === 'object' && !Array.isArray(receipt)
    && receipt.operation_id === operation
    && Object.keys(scope).every(key => receipt[key] === scope[key])
}

/** Only real successful write/receipt bodies introduce an operation identity. */
export function observeBusinessState(previous, scope, tool, arguments_, status, payload, source) {
  const operation = arguments_?.operation_id
  if (!['write', 'receipt'].includes(tool) || typeof operation !== 'string' || !operationId.test(operation)) return previous
  if (previous.scope !== null && !sameScope(previous.scope, scope)) throw new Error('Native business state has a different trusted scope')
  const result = payload?.result
  let observation
  if (status === 200 && result && typeof result === 'object') {
    if (tool === 'write' && matchingReceipt(result.receipt, operation, scope)) {
      observation = { receipt_confirmed: false, write_count: null, receipts: [result.receipt],
        write_acknowledged: true, last_observation: 'write_response_received_receipt_query_pending' }
    }
    if (tool === 'receipt' && result.operation_id === operation && Number.isSafeInteger(result.write_count)
      && result.write_count > 0 && Array.isArray(result.receipts) && result.receipts.length === result.write_count
      && result.receipts.every(receipt => matchingReceipt(receipt, operation, scope))) {
      observation = { receipt_confirmed: true, write_count: result.write_count, receipts: result.receipts,
        write_acknowledged: Object.hasOwn(previous.operations, operation) && previous.operations[operation].write_acknowledged,
        last_observation: 'actual_receipt_query_confirmed' }
    }
  }
  if (!observation) {
    // A failed request never invents a new operation or confirms a receipt.
    if (tool !== 'write' || !Object.hasOwn(previous.operations, operation)) return previous
    observation = { ...previous.operations[operation], receipt_confirmed: false, last_observation: 'write_result_unknown' }
  }
  const next = structuredClone(previous)
  next.scope = structuredClone(scope)
  next.operations[operation] = { ...structuredClone(observation), operation_id: operation, source: structuredClone(source) }
  next.operation_id = operation
  next.receipt_confirmed = observation.receipt_confirmed
  return next
}

/** Repeating a write invalidates a previous confirmation before forwarding. */
export function beginBusinessWrite(previous, arguments_, source) {
  const operation = arguments_?.operation_id
  if (typeof operation !== 'string' || !Object.hasOwn(previous.operations, operation)) return previous
  const next = structuredClone(previous)
  next.operations[operation] = { ...next.operations[operation], receipt_confirmed: false,
    last_observation: 'write_in_flight', source: structuredClone(source) }
  next.operation_id = operation
  next.receipt_confirmed = false
  return next
}

export async function mountBusinessExtension(ctx, { scope, quantityLimit, append, save, writeNativeRecord }) {
  if (quantityLimit !== 3) throw new Error('Expected the frozen extension quantity limit')
  const registry = ctx.get('sessionProjections')
  const agent = ctx.agent
  if (!registry || !agent) throw new Error('Business extension requires the public session projection registry and actual Agent scope')
  // Resolve the already locked public Zod dependency of the official package.
  // This function runs only in the candidate container, never in pure checks.
  const { z } = createRequire(new URL('../sources/main/packages/session/session-projection/package.json', import.meta.url))('zod')
  const scopeSchema = z.object({ experiment_id: z.string(), variant: z.string(), user_id: z.string(), session_id: z.string() }).strict()
  const entrySchema = z.object({ operation_id: z.string().regex(operationId), receipt_confirmed: z.boolean(),
    write_count: z.number().int().positive().nullable(), receipts: z.array(z.record(z.string(), z.unknown())),
    write_acknowledged: z.boolean(), last_observation: z.string(),
    source: z.object({ path: z.literal('http-tool-events.jsonl'), line: z.number().int().positive() }).strict(),
  }).strict()
  const stateSchema = z.object({ schema_version: z.literal(1), session_id: z.string(), scope: scopeSchema.nullable(),
    operation_id: z.string().regex(operationId).nullable(), receipt_confirmed: z.boolean(),
    operations: z.record(z.string(), entrySchema),
  }).strict().refine(value => {
    if (value.scope !== null && value.scope.session_id !== value.session_id) return false
    if (Object.entries(value.operations).some(([operation, entry]) => operation !== entry.operation_id)) return false
    if (value.operation_id === null) return value.receipt_confirmed === false && Object.keys(value.operations).length === 0
    const entry = Object.hasOwn(value.operations, value.operation_id) ? value.operations[value.operation_id] : undefined
    return value.scope !== null && entry !== undefined && value.receipt_confirmed === entry.receipt_confirmed
  }, 'Business current operation and scope must match the complete native state')
  registry.register({
    key: BUSINESS_PROJECTION, stateVersion: 2, stateSchema,
    init: header => emptyBusinessState(header.id),
    apply: (state, event) => {
      if (event.type === 'tool/call' && event.data.name === 'write') {
        // Invalidate a prior confirmation at the REAL native call boundary.
        // Retain its previous observation source until a new result settles.
        let args
        try { args = JSON.parse(event.data.arguments) } catch { return state }
        const operation = args?.operation_id
        const prior = typeof operation === 'string' && Object.hasOwn(state.operations, operation)
          ? state.operations[operation] : undefined
        return prior ? beginBusinessWrite(state, args, prior.source) : state
      }
      if (event.type !== BUSINESS_EVENT || event.data.meta?.kind !== BUSINESS_META_KIND) return state
      const next = stateSchema.parse(event.data.meta.business_state)
      if (next.session_id !== state.session_id) throw new Error('Native business event has a different session identity')
      return next
    },
  })
  const readState = (session = agent.session) => {
    const state = registry.stateOf(session, BUSINESS_PROJECTION)
    if (state === undefined) throw new Error('Business projection is absent')
    const checked = stateSchema.parse(state)
    if (checked.session_id !== scope.session_id || (checked.scope !== null && !sameScope(checked.scope, scope))) throw new Error('Restored native business state does not match the trusted scope')
    return checked
  }
  ctx.tools.guard(exec => {
    if (exec.name !== 'write') return undefined
    const denial = quantityDenial(exec.arguments, quantityLimit)
    if (!denial) return undefined
    const source = append('business-tool-events.jsonl', { time: new Date().toISOString(), scope,
      call_id: String(exec.callId), tool: 'write', exposed_tool: 'write', arguments: exec.arguments,
      phase: 'finish', status: 403, http_forwarded: false, result: denial })
    append('business-rule-decisions.jsonl', { time: new Date().toISOString(), scope,
      call_id: String(exec.callId), tool: 'write', operation_id: denial.operation_id,
      decision: 'deny', reason: denial.reason, approval_required: false, layer: 'candidate_execution',
      boundary: 'scoped_tools_guard', status: 403, quantity: denial.quantity,
      quantity_limit: quantityLimit, http_forwarded: false, source })
    return denial.reason
  })
  return {
    append,
    scope,
    async beforeForward(tool, arguments_, source) {
      if (tool !== 'write') return
      // tool/call already exists. Flush its in-flight invalidation BEFORE
      // forwarding a write; no fabricated event or external state store.
      readState()
      if (!await ctx.sessions.flush(agent.session)) throw new Error('Native tool call has no persistence listener')
    },
    async observe(tool, arguments_, status, payload, source) {
      const previous = readState()
      // This snapshot becomes part of the tool's canonical output. The pure
      // presentationMeta projection below carries it to the scheduler's real
      // tool/result; only that committed event changes the public projection.
      return stateSchema.parse(observeBusinessState(previous, scope, tool, arguments_, status, payload, source))
    },
    presentationMeta(_arguments, value) {
      return { kind: BUSINESS_META_KIND, business_state: stateSchema.parse(value.business_state) }
    },
    snapshot(session, stage) {
      const state = readState(session)
      const nativeEvent = session.events.findLast(event => event.type === BUSINESS_EVENT && event.data.meta?.kind === BUSINESS_META_KIND)
      const evidence = { stage, scope, state, projection_key: BUSINESS_PROJECTION, native_event_type: BUSINESS_EVENT,
        native_session_next_seq: session.seq, last_state_event_seq: nativeEvent?.seq ?? null,
        last_state_event_sha256: nativeEvent ? createHash('sha256').update(JSON.stringify(nativeEvent)).digest('hex') : null,
        state_sha256: createHash('sha256').update(JSON.stringify(state)).digest('hex'),
        source: 'public_sessionProjections_stateOf_on_native_session_events',
      }
      save('business-state-' + stage + '.json', evidence)
      return evidence
    },
    exportNativeState(session, rawArtifact, artifactEvidence, reconnected) {
      const state = readState(session)
      const nativeEvent = session.events.findLast(event => event.type === BUSINESS_EVENT && event.data.meta?.kind === BUSINESS_META_KIND)
      if (!nativeEvent) {
        const missing = { scope, business_state: state, source: null, status: 'no_business_observation_persisted' }
        save('business-state.json', missing)
        if (reconnected) save('business-state-reconnected.json', missing)
        return
      }
      // Copy one actual stored record verbatim, never serialize a replacement
      // log. The external grader follows /data/meta/business_state and hashes
      // the complete native record, including its real call/result identity.
      const lines = rawArtifact.content.split('\n')
      const index = lines.findIndex(line => {
        if (!line) return false
        const record = JSON.parse(line)
        return record.type === BUSINESS_EVENT && record.seq === nativeEvent.seq
      })
      if (index < 0 || index === lines.length - 1) throw new Error('Native business event has no complete stored JSONL record')
      const record = JSON.parse(lines[index])
      if (!isDeepStrictEqual(record.data, nativeEvent.data) || !isDeepStrictEqual(record.data.meta?.business_state, state)) throw new Error('Stored native business metadata differs from the public projection')
      const exactRecord = lines[index] + '\n'
      const filename = 'native-business-state-record.jsonl'
      writeNativeRecord(filename, exactRecord)
      const source = { path: filename, sha256: createHash('sha256').update(exactRecord).digest('hex'), line: 1, json_pointer: BUSINESS_STATE_POINTER }
      const evidence = { scope, business_state: state, source,
        native_origin: { path: artifactEvidence.native_location.path, line: index + 1,
          physical_sha256: artifactEvidence.physical_sha256,
          decoded_artifact_sha256: artifactEvidence.decoded_artifact_sha256,
          session_id: String(session.id), event_seq: nativeEvent.seq },
        read_api: reconnected ? 'native agents.resume + sessionProjections.stateOf' : 'native Session + sessionProjections.stateOf' }
      save('business-state.json', evidence)
      if (reconnected) save('business-state-reconnected.json', evidence)
    },
  }
}
