"""Data-only evidence for a quiescent Worker exit1, never task success.

The host, independent bench and batch reader recheck these same source bytes.
No candidate SDK, model, tool or executable native-state loader is executed. Unsupported catch
paths remain evidence failures rather than being guessed from error text.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import sqlite3
import tempfile

from runner.dsh_goal import fold_durable_goal, matches_live_view


def need(condition, code):
    if not condition:
        raise ValueError('fault_controlled_failure_' + code)


def digest(data):
    return hashlib.sha256(data).hexdigest()


def decoded(data):
    def pairs(rows):
        result = {}
        for key, value in rows:
            need(key not in result, 'duplicate_json_key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('fault_controlled_failure_nonfinite_json')
    return json.loads(data, object_pairs_hook=pairs, parse_constant=constant)


class Sources:
    def __init__(self, run, fixture, state):
        self.roots = {'run': Path(run), 'fixture': Path(fixture), 'state': Path(state)}
        self.hashes = {}

    def bytes(self, root, name):
        need(isinstance(name, str), 'source_path')
        relative = PurePosixPath(name)
        need(relative.parts and not relative.is_absolute()
             and '..' not in relative.parts and relative.as_posix() == name, 'source_path')
        path = self.roots[root].joinpath(*relative.parts)
        need(path.is_file() and not any(item.is_symlink() for item in (path, *path.parents))
             and path.stat().st_size <= 64 * 1024**2, 'source_regular_file')
        data = path.read_bytes()
        need(len(data) <= 64 * 1024**2, 'source_size')
        self.hashes[root + '/' + name] = digest(data)
        return data

    def json(self, root, name):
        value = decoded(self.bytes(root, name))
        need(isinstance(value, dict), 'source_json_object')
        return value

    def jsonl(self, root, name):
        data = self.bytes(root, name)
        need(bool(data) and data.endswith(b'\n'), 'source_jsonl_terminal')
        values = [decoded(line) for line in data.splitlines()]
        need(all(isinstance(row, dict) for row in values), 'source_jsonl_objects')
        return values


def stopped(snapshot):
    state = snapshot.get('state', {})
    return (state.get('Status') == 'exited' and type(state.get('Pid')) is int and state['Pid'] == 0
        and type(state.get('ExitCode')) is int and state['ExitCode'] == 1
        and all(state.get(key) is False for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
        and state.get('Error') == '')


def controlled_failure(run, fixture, scope, pins, candidate, attempt, actual, *, state):
    """Return bound failure facts, or reject incomplete/unhealthy evidence.

`incomplete` includes a wrapper refusing completion although the native turn
completed. That remains an observed unsuccessful attempt, never a success or
an assertion that the candidate honestly resolved business uncertainty.
"""
    need(candidate in {'deepagents_langgraph', 'deepseek_harness'}
         and attempt in {'worker-a', 'worker-b'} and isinstance(actual, dict) and stopped(actual), 'process_exit1')
    need(pins.get('candidate') == candidate and pins.get('scope') == scope
         and pins.get('chain_id') == scope.get('variant'), 'pins_scope')
    source = Sources(run, fixture, state)
    stored_pins = source.json('run', 'trusted/pins.json')
    need(stored_pins == pins, 'pins_changed')
    created_name = 'trusted/' + attempt + '-created.json'
    created = source.json('run', created_name)
    before = created.get('before_start', {})
    need(all(created.get(key) == value for key, value in {
        'schema': 'harness-lab-fault-worker/1', 'candidate': candidate, 'scope': scope,
        'chain_id': pins['chain_id'], 'attempt_id': attempt}.items())
        and created.get('kill') is None and created.get('errors') == [], 'created_identity')
    need(isinstance(actual.get('id'), str) and re.fullmatch('[0-9a-f]{64}', actual['id'])
         and before.get('id') == actual['id'], 'exact_id')
    for item in (before, actual):
        need(item.get('name') == '/' + pins['worker_names'][attempt]
             and item.get('image_id') == pins['candidate_image_id']
             and item.get('image_tag') == pins['candidate_image_id']
             and item.get('memory_limit') == 2 * 1024**3 and item.get('cpu_nano') == 10**9, 'image_name_caps')
    need(before.get('state', {}).get('Status') == 'created'
         and before['state'].get('Running') is False, 'created_state')
    binding_name = 'trusted/' + attempt + '-binding.json'
    binding = source.json('run', binding_name)
    need(all(binding.get(key) == value for key, value in {
        'schema': 'harness-lab-fault-worker-binding/1', 'candidate': candidate, 'scope': scope,
        'chain_id': pins['chain_id'], 'attempt_id': attempt, 'candidate_image_id': pins['candidate_image_id'],
        'mode': 'native-start' if attempt == 'worker-a' else 'native-crash-resume'}.items()), 'binding_identity')
    need(created.get('binding_sha256') == source.hashes['run/' + binding_name]
         and binding.get('pins_sha256') == source.hashes['run/trusted/pins.json'], 'binding_hash')
    request_name = 'attempts/' + attempt + '/request.json'
    request = source.json('fixture', request_name)
    original = source.json('fixture', 'candidate-request.json')
    frozen_scope = source.json('fixture', 'scope.json')
    need(frozen_scope == scope and binding.get('scope_sha256') == source.hashes['fixture/scope.json']
         and binding.get('request_sha256') == source.hashes['fixture/' + request_name]
         and request.get('run_id') == pins['chain_id'] and request.get('session_id') == scope['session_id'], 'request_identity')
    need(request.get('prompt') == original.get('prompt') if attempt == 'worker-a' else
         'prompt' not in request and 'followup' not in request
         and 'followup' not in request.get('candidate_options', {}), 'original_input')
    controls = {'native-crash.json'} if candidate == 'deepagents_langgraph' else {'fault-goal.json', 'fault-state.json'}
    need(set(binding.get('native_control_files', [])) == controls, 'controls')
    for name in sorted(controls):
        source.bytes('fixture', 'attempts/' + attempt + '/' + name)
        need(binding.get('control_sha256', {}).get(name) == source.hashes['fixture/attempts/' + attempt + '/' + name], 'control_hash')
    directory = 'attempts/' + attempt + '/candidate/'
    result = source.json('run', directory + 'result.json')
    entry = source.json('run', directory + 'entrypoint.json')
    need(result.get('status') in {'failed', 'incomplete'} and not result.get('error')
         and not result.get('error_type') and result.get('stop_reason') not in {
             'runtime_error', 'active_timeout', 'native_close_failed', 'no_native_result'}, 'native_result')
    need(all(entry.get(key) == value for key, value in {
        'scope': scope, 'run_id': pins['chain_id'], 'session_id': scope['session_id']}.items())
         and result.get('session_id') == scope['session_id']
         and all(entry.get('provenance', {}).get(key) == pins.get(key)
                 for key in ('source_export_sha256', 'adapter_export_sha256')), 'entrypoint_identity')
    if candidate == 'deepseek_harness':
        dsh(source, directory, request, original, result, entry, scope, pins, attempt)
        native_checkpoint = None
    else:
        native_checkpoint = da(source, directory, request, original, result, entry, scope)
    facts = {'schema': 'harness-lab-controlled-failure/1', 'kind': 'controlled_failure',
        'candidate': candidate, 'scope': scope, 'chain_id': pins['chain_id'], 'attempt_id': attempt,
        'container_id': actual['id'], 'candidate_image_id': pins['candidate_image_id'],
        'result_sha256': source.hashes['run/' + directory + 'result.json'], 'sources': source.hashes,
        'task_success_implied': False, 'honest_failure_or_confirmation_implied': False}
    if native_checkpoint is not None:
        facts['native_checkpoint'] = native_checkpoint
    return facts


def dsh(source, directory, request, original, result, entry, scope, pins, attempt):
    need(entry.get('variant') == 'fault-goal-runtime' and entry.get('profile') == 'harness-lab-fault-goal'
         and result.get('status') == 'incomplete', 'dsh_wrapper_status')
    control = result.get('fault_recovery', {})
    need(all(control.get(key) == value for key, value in request['fault_recovery'].items())
         and control.get('chain_id') == pins['chain_id'] and control.get('attempt_id') == attempt
         and control.get('image_verification') == 'trusted_declaration_match_only_host_must_verify_actual_docker_image_id', 'dsh_control')
    terminal = source.json('run', directory + 'native-fault-terminal.json')
    artifact = source.json('run', directory + 'session-artifact-after-flush.json')
    idle = source.json('run', directory + 'session-after-idle.json')
    index = source.json('run', directory + 'native-state-index-after-close.json')
    native = result.get('native_result', {})
    outcome = native.get('faultOutcome', {})
    need(terminal.get('schema') == 'harness-lab-dsh-fault-terminal/1'
         and terminal.get('scope') == scope and terminal.get('chain_id') == pins['chain_id']
         and terminal.get('session_id') == scope['session_id'] and terminal.get('kind') == 'incomplete'
         and outcome == {key: terminal[key] for key in ('kind', 'goal', 'native_errors')}
         and type(terminal.get('native_errors')) is int and terminal['native_errors'] >= 0
         and type(terminal.get('concurrent_input')) is bool and terminal.get('business_recovery_verified_by_candidate') is False,
         'dsh_terminal_identity')
    live_header, persisted_header = dict(idle.get('header', {})), dict(artifact.get('header', {}))
    # The public live Session omits the persistence-only default depth on a
    # newly created session. Preserve every other field and any nonzero depth.
    live_header.setdefault('delegationDepth', 0)
    persisted_header.setdefault('delegationDepth', 0)
    need(artifact == terminal.get('artifact') == native.get('artifact') and artifact.get('scope') == scope
         and artifact.get('stage') == 'after-flush' and artifact.get('header', {}).get('id') == scope['session_id']
         and artifact['header'].get('cwd') == '/workspace' and idle.get('scope') == scope
         and idle.get('agent_status') == 'idle' and idle.get('inbox', {}).get('has_pending') is False
         and live_header == persisted_header and native.get('sessionId') == scope['session_id'], 'dsh_quiescent_artifact')
    location = artifact.get('native_location', {})
    need(location.get('kind') == 'jsonl' and isinstance(location.get('path'), str)
         and location['path'].startswith('/state/'), 'dsh_state_location')
    relative = location['path'].removeprefix('/state/')
    physical = source.bytes('state', relative)
    need(physical.endswith(b'\n') and len(physical) == artifact.get('physical_bytes')
         and digest(physical) == artifact.get('physical_sha256') == artifact.get('decoded_artifact_sha256')
         and len(physical) == artifact.get('decoded_artifact_bytes')
         and index.get('schema') == 'harness-lab-native-state-index/1', 'dsh_physical_bytes')
    matching = [row for row in index.get('files', []) if row.get('root') == 'state' and row.get('path') == relative]
    need(matching == [{'root': 'state', 'path': relative, 'bytes': len(physical), 'sha256': digest(physical)}], 'dsh_after_close_state')
    rows = [decoded(line) for line in physical.splitlines()]
    need(rows and isinstance(rows[0], dict), 'dsh_header')
    header = {key: value for key, value in rows[0].items() if key != 'type'}
    need(rows[0].get('type') == 'session' and header == artifact['header'], 'dsh_original_header')
    events = rows[1:]
    need(all(isinstance(row, dict) and type(row.get('seq')) is int and row['seq'] == i
             for i, row in enumerate(events)), 'dsh_native_sequence')
    goal = fold_durable_goal(events)
    need(goal and goal['objective'] == original['prompt']
         and terminal['goal'].get('activation') in {'armed', 'disarmed'}
         and matches_live_view(terminal['goal'], goal, terminal['goal'].get('activation')), 'dsh_original_goal')
    raw = source.jsonl('run', directory + 'raw-events.jsonl')
    observed = []
    for row in raw:
        need(row.get('method') == 'session.event' and isinstance(row.get('params'), dict)
             and row['params'].get('sessionId') == scope['session_id'], 'dsh_raw_event')
        event = row['params'].get('event', {})
        seq = event.get('seq')
        need(type(seq) is int and 0 <= seq < len(events)
             and all(event.get(key) == events[seq].get(key) for key in ('seq', 'time', 'type', 'data')), 'dsh_raw_physical_source')
        observed.append(event)
    sequences = [event['seq'] for event in observed]
    need(sequences == sorted(set(sequences)), 'dsh_raw_event_order')
    end = terminal.get('last_native_turn_end', {})
    ref = end.get('source', {})
    line = ref.get('line')
    native_ends = [event for event in events if event.get('type') == 'turn/end']
    need(ref.get('path') == 'raw-events.jsonl' and type(line) is int and 1 <= line <= len(observed)
         and observed[line - 1].get('type') == 'turn/end'
         and observed[line - 1].get('seq') == end.get('seq') and observed[line - 1].get('data') == end.get('data')
         and native_ends and native_ends[-1] == events[end['seq']], 'dsh_terminal_source')
    reason = end.get('data', {}).get('reason', {}).get('kind')
    need(isinstance(reason, str) and result.get('stop_reason') == reason
         and result.get('native_status') == ('completed' if reason == 'completed' else 'incomplete'), 'dsh_native_status')
    faults = source.jsonl('run', directory + 'fault-events.jsonl')
    need(all(row.get('scope') == scope and row.get('chain_id') == pins['chain_id']
             and row.get('attempt_id') == attempt for row in faults), 'dsh_fault_event_identity')
    closes = [row for row in faults if row.get('phase') == 'native_normal_close_observed']
    need(len(closes) == 1 and closes[0] == faults[-1] and closes[0].get('normal_close') is True
         and closes[0].get('native_terminal_kind') == 'incomplete', 'dsh_normal_close')


def da_checkpoint_metadata(config, metadata):
    """Pinned checkpoint/base.get_checkpoint_metadata, as plain JSON data.

    SQLite calls this before writing metadata; checkpoint stream events expose
    the earlier loop metadata. Preserve its precedence, NUL removal and exact
    excluded/private/non-scalar configuration rules.
    """
    need(isinstance(config, dict) and isinstance(metadata, dict), 'da_checkpoint_metadata_objects')
    excluded = {'thread_id', 'checkpoint_id', 'checkpoint_ns', 'checkpoint_map',
        'langgraph_step', 'langgraph_node', 'langgraph_triggers', 'langgraph_path', 'langgraph_checkpoint_ns'}
    merged = {key: value.replace('\0', '') if isinstance(value, str) else value for key, value in metadata.items()}
    for values in (config.get('metadata'), config.get('configurable')):
        if not values:
            continue
        need(isinstance(values, dict), 'da_checkpoint_metadata_config')
        for key, value in values.items():
            if key in merged or key in excluded or key.startswith('__'):
                continue
            if isinstance(value, str):
                merged[key] = value.replace('\0', '')
            elif isinstance(value, (int, bool, float)):
                merged[key] = value
    return merged


def da(source, directory, request, original, result, entry, scope):
    need(entry.get('variant') == 'fault-recovery' and entry.get('factory') == 'deepagents.create_deep_agent'
         and entry.get('pause_hooks_installed') is False and entry.get('durability') == 'sync'
         and entry.get('fault_recovery') == request['fault_recovery']
         and result.get('scope') == scope and result.get('run_id') == scope['variant']
         and result.get('native_status') == result['status'], 'da_native_identity')
    snapshot = source.json('run', directory + 'state-snapshot.json')
    index = source.json('run', directory + 'evidence-index.json')
    need(index.get('scope') == scope, 'da_evidence_identity')
    raw = source.jsonl('run', directory + 'raw-events.jsonl')
    need(index.get('files', {}).get('raw-events.jsonl', {}).get('sha256') == source.hashes['run/' + directory + 'raw-events.jsonl']
         and index['files']['raw-events.jsonl'].get('lines') == len(raw)
         and all(row.get('scope') == scope and row.get('run_id') == scope['variant']
                 and row.get('session_id') == scope['session_id'] for row in raw), 'da_native_event_hash')
    configurable = snapshot.get('config', {}).get('configurable', {})
    need(configurable.get('thread_id') == scope['session_id'] and configurable.get('checkpoint_ns', '') == ''
         and configurable.get('checkpoint_id') == result.get('checkpoint_id') and result.get('checkpoint_id')
         and snapshot.get('values') == result.get('native_result'), 'da_native_snapshot')
    messages = snapshot.get('values', {}).get('messages', [])
    need(isinstance(messages, list) and messages and messages[0].get('content') == original['prompt'], 'da_original_objective')
    last = messages[-1]
    metadata = last.get('response_metadata', {})
    tasks = snapshot.get('tasks', [])
    need(isinstance(tasks, list) and all(isinstance(row, dict) or isinstance(row, list) and len(row) == 7 for row in tasks), 'da_native_tasks')
    # Limit this change to quiescent terminal-message failures. Pending task /
    # interrupt outcomes need a separate reviewed physical-state decoder.
    need(tasks == [] and snapshot.get('next') == [] and not snapshot.get('interrupts')
         and last.get('type') == 'ai' and not last.get('tool_calls') and not last.get('invalid_tool_calls'), 'da_unreviewed_pending_state')
    errors = [row.get('error') if isinstance(row, dict) else row[3] for row in tasks]
    interrupts = [row.get('interrupts') if isinstance(row, dict) else row[4] for row in tasks]
    finish = metadata.get('finish_reason')
    if any(errors) or metadata.get('error'):
        expected_status, reason = 'failed', 'native_task_error'
    elif snapshot.get('next') or any(interrupts) or last.get('type') != 'ai' or last.get('tool_calls') or last.get('invalid_tool_calls'):
        expected_status, reason = 'incomplete', 'pending_graph_state'
    elif finish == 'length':
        expected_status, reason = 'incomplete', 'length'
    elif finish in {'error', 'failed', 'timeout', 'cancelled', 'canceled', 'aborted'}:
        expected_status, reason = 'failed', finish
    else:
        raise ValueError('fault_controlled_failure_da_unreviewed_stop')
    need(result['status'] == expected_status and result.get('stop_reason') == reason
         and result.get('response_metadata') == metadata and result.get('finish_reason') == finish,
         'da_native_failure_source')
    need(result.get('native_terminal') == {'finish_reason': finish, 'response_metadata': metadata,
         'pending_tool_calls': bool(last.get('tool_calls') or last.get('invalid_tool_calls')),
         'is_streaming': False, 'error_message': 'native_task_error' if expected_status == 'failed' else None},
         'da_native_terminal')
    checkpoints = [row['data'] for row in raw if row.get('stream_mode') == 'checkpoints'
        and isinstance(row.get('data'), dict) and row['data'].get('config', {}).get('configurable', {}).get('checkpoint_id') == result['checkpoint_id']]
    need(checkpoints and all(checkpoints[-1].get(key) == snapshot.get(key)
         for key in ('values', 'next', 'tasks'))
         and checkpoints[-1].get('config', {}).get('configurable', {}).get('thread_id') == scope['session_id']
         and checkpoints[-1].get('config', {}).get('configurable', {}).get('checkpoint_ns', '') == '', 'da_checkpoint_stream_source')
    # create_deep_agent.with_config supplies this one scalar metadata value.
    # lc_versions is a mapping and lc_agent_name defaults to None, so the
    # pinned saver excludes both. rm_pregel_keys omits metadata in the stream.
    expected_metadata = da_checkpoint_metadata({'metadata': {'ls_integration': 'deepagents'},
        'configurable': checkpoints[-1]['config']['configurable']}, checkpoints[-1].get('metadata'))
    need(snapshot.get('metadata') == expected_metadata, 'da_checkpoint_metadata_merge')
    database = source.bytes('state', 'checkpoints.sqlite')
    wal = None
    for suffix in ('-wal',):
        path = source.roots['state'] / ('checkpoints.sqlite' + suffix)
        if path.exists() or path.is_symlink():
            wal = source.bytes('state', 'checkpoints.sqlite' + suffix)
    # Open only copies: even mode=ro can create an SQLite shared-memory file
    # next to a WAL database. Candidate state remains byte-for-byte untouched.
    with tempfile.TemporaryDirectory(prefix='fault-outcome-') as temporary:
        copied = Path(temporary) / 'checkpoints.sqlite'
        copied.write_bytes(database)
        if wal is not None:
            copied.with_name(copied.name + '-wal').write_bytes(wal)
        with sqlite3.connect(copied.as_uri() + '?mode=ro', uri=True) as connection:
            connection.execute('PRAGMA query_only=ON')
            need(connection.execute('PRAGMA quick_check').fetchall() == [('ok',)]
                 and {row[1] for row in connection.execute('PRAGMA table_info(checkpoints)')} == {
                     'thread_id', 'checkpoint_ns', 'checkpoint_id', 'parent_checkpoint_id', 'type', 'checkpoint', 'metadata'}
                 and {row[1] for row in connection.execute('PRAGMA table_info(writes)')} == {
                     'thread_id', 'checkpoint_ns', 'checkpoint_id', 'task_id', 'task_path', 'idx', 'channel', 'type', 'value'}, 'da_physical_schema')
            need(connection.execute('SELECT DISTINCT thread_id,checkpoint_ns FROM checkpoints').fetchall() == [(scope['session_id'], '')]
                 and connection.execute('SELECT COUNT(*) FROM writes WHERE thread_id!=? OR checkpoint_ns!=?', (scope['session_id'], '')).fetchone()[0] == 0,
                 'da_physical_thread')
            found = connection.execute('SELECT checkpoint_id,type,checkpoint,metadata FROM checkpoints WHERE thread_id=? AND checkpoint_ns=? ORDER BY checkpoint_id DESC LIMIT 1',
                (scope['session_id'], '')).fetchone()
    need(found and found[0] == result['checkpoint_id'] and found[1] == 'msgpack'
         and isinstance(found[2], bytes) and found[2] and isinstance(found[3], bytes), 'da_physical_checkpoint')
    try:
        physical_metadata = decoded(found[3])
    except (ValueError, TypeError, UnicodeDecodeError):
        raise ValueError('fault_controlled_failure_da_physical_metadata_json') from None
    need(physical_metadata == expected_metadata, 'da_physical_metadata')
    return {'checkpoint_id': found[0], 'checkpoint_blob_sha256': digest(found[2]), 'metadata_sha256': digest(found[3]),
            'payload_decoding': 'independent_bench_required'}
