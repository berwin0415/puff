"""I23 纯数据退出码1校准初稿，不调用 Docker 或模型。"""
from argparse import Namespace
from copy import deepcopy
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from runner import fault_outcome as outcome
from runner import fault_recovery as bench


SCOPE = {'experiment_id': 'fault-recovery', 'variant': 'failure-data', 'user_id': 'alice', 'session_id': 'main'}
IMAGE = 'sha256:' + 'a' * 64
CONTAINER = 'b' * 64


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


def checksum(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture(params=[('deepseek_harness', 'completed'), ('deepagents_langgraph', 'length'), ('deepagents_langgraph', 'timeout')])
def failure(tmp_path, request):
    candidate, native_reason = request.param
    paths = SimpleNamespace(**{name: tmp_path / name for name in ('run', 'fixture', 'state', 'workspace', 'budget')})
    for path in vars(paths).values():
        path.mkdir()
    directory = paths.run / 'attempts/worker-a/candidate'
    directory.mkdir(parents=True)
    controls = ['fault-goal.json', 'fault-state.json'] if candidate == 'deepseek_harness' else ['native-crash.json']
    protected = {'run_id': SCOPE['variant'], 'session_id': 'main', 'prompt': 'original objective',
                 'fault_recovery': {'mode': 'new', 'chain_id': SCOPE['variant'], 'attempt_id': 'worker-a'}}
    save(paths.fixture / 'candidate-request.json', protected)
    save(paths.fixture / 'attempts/worker-a/request.json', protected)
    save(paths.fixture / 'scope.json', SCOPE)
    (paths.fixture / 'scope.token').write_text('scope-token')
    (paths.budget / 'run.token').write_text('model-token')
    for name in controls:
        save(paths.fixture / 'attempts/worker-a' / name, {})
    pins = {'candidate': candidate, 'scope': SCOPE, 'chain_id': SCOPE['variant'], 'candidate_image_id': IMAGE,
        'worker_names': {'worker-a': 'exact-a'}, 'source_export_sha256': 'c' * 64, 'adapter_export_sha256': 'd' * 64}
    save(paths.run / 'trusted/pins.json', pins)
    binding = {'schema': 'harness-lab-fault-worker-binding/1', 'candidate': candidate, 'scope': SCOPE,
        'chain_id': SCOPE['variant'], 'attempt_id': 'worker-a', 'candidate_image_id': IMAGE, 'mode': 'native-start',
        'native_control_files': controls, 'pins_sha256': checksum(paths.run / 'trusted/pins.json'),
        'scope_sha256': checksum(paths.fixture / 'scope.json'),
        'scope_token_sha256': checksum(paths.fixture / 'scope.token'),
        'model_token_sha256': checksum(paths.budget / 'run.token'),
        'request_sha256': checksum(paths.fixture / 'attempts/worker-a/request.json'),
        'control_sha256': {name: checksum(paths.fixture / 'attempts/worker-a' / name) for name in controls}}
    save(paths.run / 'trusted/worker-a-binding.json', binding)
    mounts = {'/config/request.json': (paths.fixture / 'attempts/worker-a/request.json', False),
        '/config/scope.json': (paths.fixture / 'scope.json', False), '/config/scope.token': (paths.fixture / 'scope.token', False),
        '/budget/run.token': (paths.budget / 'run.token', False), '/state': (paths.state, True),
        '/workspace': (paths.workspace, True), '/evidence': (directory, True),
        **{'/config/' + name: (paths.fixture / 'attempts/worker-a' / name, False) for name in controls}}
    before = {'id': CONTAINER, 'name': '/exact-a', 'image_id': IMAGE, 'image_tag': IMAGE,
        'memory_limit': 2 * 1024**3, 'cpu_nano': 10**9,
        'mounts': [{'Source': str(path), 'Destination': dest, 'RW': writable} for dest, (path, writable) in mounts.items()],
        'state': {'Status': 'created', 'Pid': 0, 'ExitCode': 0, 'Running': False, 'OOMKilled': False,
                  'Dead': False, 'Paused': False, 'Restarting': False, 'Error': ''}}
    actual = deepcopy(before)
    actual['state'].update(Status='exited', ExitCode=1)
    created = {'schema': 'harness-lab-fault-worker/1', 'candidate': candidate, 'scope': SCOPE,
        'chain_id': SCOPE['variant'], 'attempt_id': 'worker-a', 'errors': [], 'kill': None,
        'binding_sha256': checksum(paths.run / 'trusted/worker-a-binding.json'), 'before_start': before}
    save(paths.run / 'trusted/worker-a-created.json', created)
    save(paths.run / 'trusted/worker-a.json', {**created, 'after_exit': actual, 'timed_out': False,
                                            'attach_exit': 1, 'process_exit': 1})
    entry = {'scope': SCOPE, 'run_id': SCOPE['variant'], 'session_id': 'main',
        'provenance': {key: pins[key] for key in ('source_export_sha256', 'adapter_export_sha256')}}
    if candidate == 'deepseek_harness':
        entry.update(variant='fault-goal-runtime', profile='harness-lab-fault-goal')
        header = {'version': 0, 'id': 'main', 'cwd': '/workspace', 'createdAt': 1, 'delegationDepth': 0}
        initial = {'id': 'original-goal', 'revision': 1, 'objective': protected['prompt'], 'phase': 'active', 'maxGoalRounds': 20}
        final = {**initial, 'revision': 2, 'phase': 'complete'}
        def goal(operation, value):
            return {'kind': 'goal/change', 'version': 1, 'operation': operation, 'goal': value,
                    'roundsStarted': 0, 'createdAt': 1, 'updatedAt': value['revision']}
        events = [{'seq': 0, 'time': 1, 'type': 'goal/change', 'data': goal('create', initial)},
            {'seq': 1, 'time': 2, 'type': 'goal/change', 'data': goal('complete', final)},
            {'seq': 2, 'time': 3, 'type': 'turn/end', 'data': {'turn': 1, 'reason': {'kind': 'completed'}}}]
        physical = paths.state / 'sessions/main/session.jsonl'
        physical.parent.mkdir(parents=True)
        physical.write_text(''.join(json.dumps(row) + '\n' for row in [{'type': 'session', **header}, *events]))
        artifact = {'stage': 'after-flush', 'scope': SCOPE, 'header': header,
            'native_location': {'kind': 'jsonl', 'path': '/state/sessions/main/session.jsonl'},
            'physical_bytes': physical.stat().st_size, 'physical_sha256': checksum(physical),
            'decoded_artifact_bytes': physical.stat().st_size, 'decoded_artifact_sha256': checksum(physical)}
        terminal = {'schema': 'harness-lab-dsh-fault-terminal/1', 'scope': SCOPE, 'chain_id': SCOPE['variant'],
            'session_id': 'main', 'kind': 'incomplete', 'native_errors': 0, 'concurrent_input': True,
            'goal': {**final, 'roundsStarted': 0, 'createdAt': 1, 'updatedAt': 2, 'activation': 'disarmed'},
            'business_recovery_verified_by_candidate': False, 'artifact': artifact,
            'last_native_turn_end': {'seq': 2, 'data': events[-1]['data'], 'source': {'path': 'raw-events.jsonl', 'line': 3}}}
        save(directory / 'native-fault-terminal.json', terminal)
        save(directory / 'session-artifact-after-flush.json', artifact)
        save(directory / 'session-after-idle.json', {'scope': SCOPE, 'header': {k: v for k, v in header.items() if k != 'delegationDepth'},
            'agent_status': 'idle', 'inbox': {'has_pending': False}})
        save(directory / 'native-state-index-after-close.json', {'schema': 'harness-lab-native-state-index/1',
            'files': [{'root': 'state', 'path': 'sessions/main/session.jsonl', 'bytes': physical.stat().st_size, 'sha256': checksum(physical)}]})
        (directory / 'raw-events.jsonl').write_text(''.join(json.dumps({'method': 'session.event',
            'params': {'sessionId': 'main', 'event': event}}) + '\n' for event in events))
        (directory / 'fault-events.jsonl').write_text(json.dumps({'scope': SCOPE, 'chain_id': SCOPE['variant'],
            'attempt_id': 'worker-a', 'phase': 'native_normal_close_observed', 'normal_close': True,
            'native_terminal_kind': 'incomplete'}) + '\n')
        result = {'status': 'incomplete', 'native_status': 'completed', 'stop_reason': 'completed', 'session_id': 'main',
            'fault_recovery': {**protected['fault_recovery'],
                'image_verification': 'trusted_declaration_match_only_host_must_verify_actual_docker_image_id'},
            'native_result': {'sessionId': 'main', 'artifact': artifact,
                'faultOutcome': {key: terminal[key] for key in ('kind', 'goal', 'native_errors')}}}
    else:
        entry.update(variant='fault-recovery', factory='deepagents.create_deep_agent', durability='sync',
                     pause_hooks_installed=False, fault_recovery=protected['fault_recovery'])
        metadata = {'finish_reason': native_reason}
        status = 'incomplete' if native_reason == 'length' else 'failed'
        native = {'messages': [{'type': 'human', 'content': protected['prompt']},
            {'type': 'ai', 'content': 'unfinished', 'response_metadata': metadata, 'tool_calls': [], 'invalid_tool_calls': []}]}
        snapshot = {'values': native, 'next': [], 'tasks': [],
            'config': {'configurable': {'thread_id': 'main', 'checkpoint_ns': '', 'checkpoint_id': 'head'}},
            'metadata': {'source': 'loop', 'step': 1, 'parents': {}, 'ls_integration': 'deepagents'}}
        save(directory / 'state-snapshot.json', snapshot)
        streamed = deepcopy(snapshot)
        del streamed['metadata']['ls_integration']
        raw = {'scope': SCOPE, 'run_id': SCOPE['variant'], 'session_id': 'main', 'stream_mode': 'checkpoints', 'data': streamed}
        (directory / 'raw-events.jsonl').write_text(json.dumps(raw) + '\n')
        save(directory / 'evidence-index.json', {'scope': SCOPE,
            'files': {'raw-events.jsonl': {'lines': 1, 'sha256': checksum(directory / 'raw-events.jsonl')}}})
        import ormsgpack
        def message(value):
            kind = value['type']
            return ormsgpack.Ext(5, ormsgpack.packb(['langchain_core.messages.' + kind,
                'HumanMessage' if kind == 'human' else 'AIMessage', value, 'model_validate_json']))
        checkpoint = {'id': 'head', 'channel_values': {'messages': [message(row) for row in native['messages']]},
                      'channel_versions': {}, 'versions_seen': {}}
        with sqlite3.connect(paths.state / 'checkpoints.sqlite') as db:
            db.executescript('''CREATE TABLE checkpoints(
                thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, parent_checkpoint_id TEXT,
                type TEXT, checkpoint BLOB, metadata BLOB, PRIMARY KEY(thread_id,checkpoint_ns,checkpoint_id));
                CREATE TABLE writes(thread_id TEXT, checkpoint_ns TEXT, checkpoint_id TEXT, task_id TEXT,
                task_path TEXT, idx INTEGER, channel TEXT, type TEXT, value BLOB);''')
            db.execute('INSERT INTO checkpoints VALUES(?,?,?,NULL,?,?,?)', ('main', '', 'head', 'msgpack',
                ormsgpack.packb(checkpoint), json.dumps(snapshot['metadata']).encode()))
        result = {'status': status, 'native_status': status, 'stop_reason': native_reason,
            'session_id': 'main', 'scope': SCOPE, 'run_id': SCOPE['variant'], 'checkpoint_id': 'head',
            'native_result': native, 'response_metadata': metadata, 'finish_reason': native_reason,
            'native_terminal': {'finish_reason': native_reason, 'response_metadata': metadata,
                'pending_tool_calls': False, 'is_streaming': False,
                'error_message': 'native_task_error' if status == 'failed' else None}}
    save(directory / 'result.json', result)
    save(directory / 'entrypoint.json', entry)
    return SimpleNamespace(candidate=candidate, paths=paths, directory=directory, actual=actual, pins=pins,
        evaluate=lambda: outcome.controlled_failure(paths.run, paths.fixture, SCOPE, pins, candidate, 'worker-a', actual, state=paths.state))


def change_json(path, update):
    value = json.loads(path.read_bytes())
    update(value)
    save(path, value)


def test_complete_bound_failure_is_valid_evidence_but_never_a_success(failure):
    before = {p.relative_to(failure.paths.state).as_posix(): p.read_bytes()
              for p in failure.paths.state.rglob('*') if p.is_file()}
    facts = failure.evaluate()
    assert facts['kind'] == 'controlled_failure' and facts['container_id'] == CONTAINER
    assert facts['result_sha256'] == checksum(failure.directory / 'result.json')
    assert facts['task_success_implied'] is facts['honest_failure_or_confirmation_implied'] is False
    assert before == {p.relative_to(failure.paths.state).as_posix(): p.read_bytes()
                      for p in failure.paths.state.rglob('*') if p.is_file()}


@pytest.mark.parametrize('change', ['exit0', 'exit137', 'oom', 'running', 'pid', 'error', 'image', 'identity',
    'missing_result', 'completed', 'runtime_error', 'close_failed', 'foreign_scope', 'binding_hash', 'source_link'])
def test_missing_changed_or_unhealthy_failure_evidence_is_rejected(failure, change):
    actual = failure.actual
    if change == 'exit0': actual['state']['ExitCode'] = 0
    elif change == 'exit137': actual['state']['ExitCode'] = 137
    elif change == 'oom': actual['state']['OOMKilled'] = True
    elif change == 'running': actual['state']['Running'] = True
    elif change == 'pid': actual['state']['Pid'] = 12
    elif change == 'error': actual['state']['Error'] = 'runtime error'
    elif change == 'image': actual['image_id'] = 'sha256:' + 'f' * 64
    elif change == 'identity': actual['id'] = 'f' * 64
    elif change == 'missing_result': (failure.directory / 'result.json').unlink()
    elif change == 'completed': change_json(failure.directory / 'result.json', lambda row: row.update(status='completed'))
    elif change == 'runtime_error': change_json(failure.directory / 'result.json', lambda row: row.update(stop_reason='runtime_error', error_type='RuntimeError'))
    elif change == 'close_failed': change_json(failure.directory / 'result.json', lambda row: row.update(stop_reason='native_close_failed'))
    elif change == 'foreign_scope': change_json(failure.directory / 'entrypoint.json', lambda row: row.update(scope={**SCOPE, 'user_id': 'foreign'}))
    elif change == 'binding_hash': change_json(failure.paths.run / 'trusted/worker-a-binding.json', lambda row: row.update(request_sha256='0' * 64))
    else:
        path = failure.directory / 'result.json'
        path.rename(path.with_name('result-retained.json'))
        path.symlink_to(path.with_name('result-retained.json'))
    with pytest.raises((ValueError, OSError)):
        failure.evaluate()


@pytest.mark.parametrize('change', ['raw_hash', 'normal_close', 'goal_identity', 'native_terminal', 'physical_state'])
def test_candidate_projection_cannot_replace_native_failure_source(failure, change):
    if change == 'raw_hash':
        (failure.directory / 'raw-events.jsonl').write_text('{}\n')
    elif change == 'physical_state':
        path = next(p for p in failure.paths.state.rglob('*') if p.is_file())
        path.write_bytes(b'changed physical state')
    elif failure.candidate == 'deepseek_harness':
        if change == 'normal_close':
            change_json(failure.directory / 'fault-events.jsonl', lambda row: row.update(normal_close=False))
        elif change == 'goal_identity':
            change_json(failure.directory / 'native-fault-terminal.json', lambda row: row['goal'].update(id='foreign'))
        else:
            (failure.directory / 'native-fault-terminal.json').unlink()
    else:
        name = 'state-snapshot.json' if change in {'normal_close', 'goal_identity'} else 'result.json'
        change_json(failure.directory / name, lambda row: row.update(native_terminal={}) if name == 'result.json' else row['config']['configurable'].update(checkpoint_id='foreign'))
    with pytest.raises((ValueError, OSError, sqlite3.Error)):
        failure.evaluate()


def test_bench_process_reader_independently_checks_exit1_native_sources(failure, monkeypatch):
    monkeypatch.setattr(bench, 'load_config', lambda: SimpleNamespace(for_environment=lambda _: object()))
    monkeypatch.setattr(bench.io, 'source_on_host', lambda config, path: str(path))
    args = Namespace(candidate=failure.candidate, chain_id=SCOPE['variant'], fault='none')
    result = bench.process_evidence(failure.paths, SCOPE, failure.pins, args)
    assert result['worker-a']['controlled_failure'] == failure.evaluate()
    assert result['worker-a']['controlled_failure_native']['physical_native_failure_verified'] is True
    change_json(failure.directory / 'result.json', lambda row: row.update(status='completed'))
    with pytest.raises(ValueError):
        bench.process_evidence(failure.paths, SCOPE, failure.pins, args)


def test_bench_rejects_forged_da_snapshot_even_when_public_sources_agree(failure, monkeypatch):
    if failure.candidate != 'deepagents_langgraph':
        pytest.skip('DA 物理检查点边界')
    monkeypatch.setattr(bench, 'load_config', lambda: SimpleNamespace(for_environment=lambda _: object()))
    monkeypatch.setattr(bench.io, 'source_on_host', lambda config, path: str(path))
    snapshot = json.loads((failure.directory / 'state-snapshot.json').read_bytes())
    snapshot['values']['messages'][-1]['content'] = 'forged public terminal'
    save(failure.directory / 'state-snapshot.json', snapshot)
    change_json(failure.directory / 'result.json', lambda row: row.update(native_result=snapshot['values']))
    raw_path = failure.directory / 'raw-events.jsonl'
    change_json(raw_path, lambda row: row.update(data=snapshot))
    change_json(failure.directory / 'evidence-index.json', lambda row: row['files']['raw-events.jsonl'].update(sha256=checksum(raw_path)))
    # 宿主不导入 MessagePack，只冻结公开来源与物理 BLOB 哈希；
    # 独立验收程序仍须解码原始 BLOB。
    failure.evaluate()
    with pytest.raises(ValueError, match='physical_messages_differ'):
        bench.process_evidence(failure.paths, SCOPE, failure.pins,
            Namespace(candidate=failure.candidate, chain_id=SCOPE['variant'], fault='none'))


def test_official_metadata_merge_preserves_precedence_filters_and_string_normalization():
    metadata = {'source': 'lo\0op', 'step': 3, 'parents': {}, 'existing': 'native\0value'}
    config = {'metadata': {'ls_integration': 'deepagents', 'existing': 'ignored', 'lc_versions': {'deepagents': 'fixed'},
        'lc_agent_name': None, 'thread_id': 'excluded', '__private': 'excluded', 'flag': False, 'number': 1.5},
        'configurable': {'thread_id': 'main', 'checkpoint_ns': '', 'checkpoint_id': 'head',
            'ls_integration': 'ignored', 'extra': 'config\0value', 'nested': {'ignored': True}}}
    assert outcome.da_checkpoint_metadata(config, metadata) == {
        'source': 'loop', 'step': 3, 'parents': {}, 'existing': 'nativevalue',
        'ls_integration': 'deepagents', 'flag': False, 'number': 1.5, 'extra': 'configvalue'}
    assert metadata['source'] == 'lo\0op'


@pytest.mark.parametrize('change', ['snapshot_source', 'snapshot_extra', 'snapshot_integration',
    'physical_source', 'physical_extra', 'physical_integration', 'physical_non_json', 'stream_and_snapshot'])
def test_da_metadata_must_match_official_merge_and_original_physical_row(failure, change):
    if failure.candidate != 'deepagents_langgraph':
        pytest.skip('DA 元数据合并边界')
    path = failure.directory / 'state-snapshot.json'
    snapshot = json.loads(path.read_bytes())
    if change.startswith('snapshot_'):
        key, value = {'snapshot_source': ('source', 'foreign'), 'snapshot_extra': ('arbitrary', True),
                      'snapshot_integration': ('ls_integration', 'foreign')}[change]
        snapshot['metadata'][key] = value
        save(path, snapshot)
    elif change == 'stream_and_snapshot':
        snapshot['metadata']['source'] = 'foreign'
        save(path, snapshot)
        raw_path = failure.directory / 'raw-events.jsonl'
        change_json(raw_path, lambda row: row['data']['metadata'].update(source='foreign'))
        change_json(failure.directory / 'evidence-index.json', lambda row: row['files']['raw-events.jsonl'].update(sha256=checksum(raw_path)))
    else:
        metadata = deepcopy(snapshot['metadata'])
        if change == 'physical_source': metadata['source'] = 'foreign'
        elif change == 'physical_extra': metadata['arbitrary'] = True
        elif change == 'physical_integration': metadata['ls_integration'] = 'foreign'
        data = b'not JSON' if change == 'physical_non_json' else json.dumps(metadata).encode()
        with sqlite3.connect(failure.paths.state / 'checkpoints.sqlite') as db:
            db.execute('UPDATE checkpoints SET metadata=?', (data,))
    with pytest.raises(ValueError, match='metadata'):
        failure.evaluate()
