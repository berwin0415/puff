"""I23 DSH 纯数据反例；真实候选恢复仍需 Docker 实验。"""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from runner import fault_recovery as f
from scripts import run_fault_recovery as host


SCOPE = {'experiment_id': 'fault-recovery', 'variant': 'dsh-crash-calibration', 'user_id': 'alice', 'session_id': 'main'}
PROMPT = 'Original objective. Do not resubmit it after a crash.'
PINS = {'candidate': 'deepseek_harness', 'chain_id': SCOPE['variant'], 'candidate_image_id': 'sha256:' + 'a' * 64,
    'source_export_sha256': 'b' * 64, 'adapter_export_sha256': 'c' * 64, 'model_name': 'frozen-model'}
ARGS = {'operation_id': 'op-crash', 'item_id': 'item-crash', 'quantity': 2}


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value) + '\n')


def events(started=True):
    goal = {'id': 'original-goal', 'revision': 1, 'objective': PROMPT, 'phase': 'active', 'maxGoalRounds': 20}
    rows = [
        {'type': 'goal/change', 'data': {'kind': 'goal/change', 'version': 1, 'operation': 'create',
            'goal': goal, 'roundsStarted': 0, 'createdAt': 10, 'updatedAt': 10}},
        {'type': 'turn/start', 'data': {'turn': 1}},
        {'type': 'agent/inbox/spliced', 'data': {'removedCount': 1, 'outcome': 'claimed', 'inserted': []}},
        {'type': 'user/message', 'data': {'source': {'kind': 'goal', 'goalId': 'original-goal', 'revision': 1, 'round': 1}}},
        {'type': 'step/start', 'data': {'turn': 1, 'step': 1}},
        {'type': 'assistant/message', 'data': {'turn': 1, 'step': 1, 'message': {'content': [
            {'type': 'tool-call', 'id': 'native-original-call', 'name': 'write', 'arguments': json.dumps(ARGS)}]}}},
    ]
    if started:
        rows.append({'type': 'tool/call', 'data': {'turn': 1, 'step': 1, 'callId': 'native-original-call',
            'name': 'write', 'arguments': json.dumps(ARGS)}})
    return [dict(row, seq=index, time=100 + index) for index, row in enumerate(rows)]


@pytest.fixture
def natural(tmp_path):
    paths = SimpleNamespace(state=tmp_path / 'state', workspace=tmp_path / 'workspace',
        run=tmp_path / 'run', fixture=tmp_path / 'fixture', budget=tmp_path / 'budget')
    for directory in (paths.state / 'sessions/project', paths.workspace, paths.run / 'trusted', paths.budget,
            paths.run / 'attempts/worker-a/candidate', paths.run / 'attempts/worker-b/candidate',
            paths.fixture / 'attempts/worker-a', paths.fixture / 'attempts/worker-b'):
        directory.mkdir(parents=True)
    save(paths.fixture / 'candidate-request.json', {'prompt': PROMPT, 'run_id': SCOPE['variant'], 'session_id': 'main',
        'timeout_seconds': 300, 'max_output_tokens': 4096, 'max_retries': 2, 'tools_url': 'http://tools:8081',
        'tool_token_file': '/config/scope.token', 'workspace': '/workspace', 'state_dir': '/state', 'output_dir': '/evidence',
        'candidate_options': {'case': 'tools', 'trusted_scope_file': '/config/scope.json'}})
    save(paths.fixture / 'scope.json', SCOPE)
    (paths.fixture / 'scope.token').write_text('local-tool-token\n')
    (paths.budget / 'run.token').write_text('original-model-token\n')
    save(paths.run / 'trusted/pins.json', PINS)
    save(paths.run / 'attempts/worker-a/candidate/entrypoint.json', {'scope': SCOPE, 'profile': 'harness-lab-fault-goal',
        'variant': 'fault-goal-runtime', 'session_action': 'create', 'session_id': 'main',
        'provenance': {key: PINS[key] for key in ('source_export_sha256', 'adapter_export_sha256')}})
    header = {'type': 'session', 'id': 'main', 'cwd': '/workspace', 'version': 1}
    physical = paths.state / 'sessions/project/main.jsonl'
    physical.write_bytes(b''.join(json.dumps(row).encode() + b'\n' for row in [header, *events()]))
    paths.physical = physical
    return paths


def test_native_repair_distinguishes_unknown_outcome_from_not_recorded_started():
    for started, code in ((True, 'TOOL_OUTCOME_UNKNOWN'), (False, 'TOOL_NOT_STARTED')):
        original = events(started)
        frozen = deepcopy(original)
        closers = f.dsh_interrupted_closers(original)
        assert original == frozen
        assert [row['type'] for row in closers] == ['tool/result', 'step/end', 'turn/end']
        assert closers[0]['data']['error']['code'] == code
        assert closers[0]['data']['message']['source']['callId'] == 'native-original-call'
        assert closers[0].get('sourceEventSeqs', []) == ([len(original) - 1] if started else [])
        assert closers[-1]['data']['reason'] == {'kind': 'interrupted'}
        assert all(row['time'] == original[-1]['time'] for row in closers)
        assert f.dsh_interrupted_closers(original + closers) == []


def test_torn_final_fragment_is_preserved_as_evidence_and_never_repaired_by_bench(natural):
    with natural.physical.open('ab') as stream:
        stream.write(b'{"type":"unfinished')
    before = natural.physical.read_bytes()
    observation = f.deepseek_crash_observation(natural, SCOPE, PINS)
    assert observation['partial_trailing_line'] is True
    assert observation['discarded_fragment_bytes'] == len(b'{"type":"unfinished')
    assert observation['physical_sha256'] == f.sha(before)
    assert observation['expected_repair_events'][0]['data']['error']['code'] == 'TOOL_OUTCOME_UNKNOWN'
    assert observation['actual_inbox_splices_in_open_tail'][0]['data']['removedCount'] == 1
    assert observation['native_session_loaded'] is False and observation['native_repair_invoked'] is False
    assert 'activation' not in observation['durable_goal']
    assert natural.physical.read_bytes() == before


@pytest.mark.parametrize('change', ['foreign_header', 'prompt', 'gap', 'malformed_committed', 'pause_index', 'wrong_profile'])
def test_foreign_corrupted_or_pause_state_cannot_become_dsh_crash_source(natural, change):
    if change == 'pause_index':
        save(natural.state / 'pause-index.json', {})
    elif change == 'wrong_profile':
        path = natural.run / 'attempts/worker-a/candidate/entrypoint.json'
        value = json.loads(path.read_text()); value['profile'] = 'harness-lab-goal'; save(path, value)
    elif change == 'prompt':
        path = natural.fixture / 'candidate-request.json'
        value = json.loads(path.read_text()); value['prompt'] = 'Replacement'; save(path, value)
    elif change == 'malformed_committed':
        with natural.physical.open('ab') as stream:
            stream.write(b'{"bad":\n')
    else:
        rows = [json.loads(row) for row in natural.physical.read_text().splitlines()]
        if change == 'foreign_header':
            rows[0]['id'] = 'foreign'
        else:
            rows[-1]['seq'] += 1
        natural.physical.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    with pytest.raises((ValueError, json.JSONDecodeError)):
        f.deepseek_crash_observation(natural, SCOPE, PINS)


def bind_b(natural, monkeypatch, *, native_call='native-original-call'):
    args = SimpleNamespace(candidate='deepseek_harness', chain_id=SCOPE['variant'])
    boundary = {'natural_native_inventory': f.io.inventory(natural.state, natural.workspace)}
    save(natural.run / 'trusted/crash-boundary.json', boundary)
    save(natural.run / 'trusted/resume-clock.json', {'chain_id': SCOPE['variant'], 'scope': SCOPE,
        'used_active_seconds': 72.25, 'boundary_sha256': f.sha(f.regular(natural.run / 'trusted/crash-boundary.json'))})
    save(natural.run / 'trusted/gate-hit.json', {'events': [{'phase': 'prewrite_hit', 'event_id': 3,
        'native_call_id': native_call, 'bridge_call_id': 'bridge-uuid-is-different'}]})
    monkeypatch.setattr(f.io, 'dsh_contract', lambda *args: 'd' * 64)
    monkeypatch.setattr(f, 'expected_target', lambda *args: ARGS)
    f.deepseek_binding(args, natural, SCOPE, PINS, boundary)
    return json.loads((natural.fixture / 'attempts/worker-b/fault-goal.json').read_text())


def test_b_keeps_budget_and_original_state_and_does_not_receive_prompt_gate_or_answers(natural, monkeypatch):
    original = natural.physical.read_bytes()
    binding = bind_b(natural, monkeypatch)
    request = json.loads((natural.fixture / 'attempts/worker-b/request.json').read_text())
    assert 'prompt' not in request and 'lifecycle' not in request
    assert request['fault_recovery']['mode'] == 'crash-resume'
    assert request['timeout_seconds'] == 227
    assert binding['active_budget'] == {'limit_seconds': 300, 'used_seconds': 72.25, 'remaining_seconds': 227}
    assert binding['model']['budget_id'] == SCOPE['variant'] and binding['model']['max_model_calls'] == 20
    assert natural.physical.read_bytes() == original
    assert not any(key in binding['recovery'] for key in ('gate_to_native_calls', 'expected_target', 'expected_repair_events'))
    audit = json.loads((natural.run / 'trusted/native-crash-dsh.json').read_text())
    assert audit['gate_to_native_calls'][0]['native_tool_call_id'] == 'native-original-call'
    assert audit['gate_to_native_calls'][0]['bridge_call_id'] != 'native-original-call'


def test_bridge_uuid_cannot_substitute_for_original_native_tool_call(natural, monkeypatch):
    with pytest.raises(ValueError, match='gate_call_not_in_original'):
        bind_b(natural, monkeypatch, native_call='bridge-uuid-is-different')
    assert not (natural.fixture / 'attempts/worker-b/request.json').exists()


def surface_lab(natural):
    lab = object.__new__(host.FaultLab)
    lab.paths = natural
    path = natural.run / 'trusted/tool-surface.json'
    save(path, {'schemas': ['synthetic-calibration-only']})
    lab.pins = {**PINS, 'tool_surface_sha256': f.sha(f.regular(path))}
    return lab


def test_surface_pin_is_persisted_before_binding_not_recomputed_inside_bench(natural):
    lab = surface_lab(natural)
    lab.freeze_calibrated_surface_pins()
    assert json.loads((natural.run / 'trusted/pins.json').read_text()) == lab.pins


@pytest.mark.parametrize('change', ['different_material', 'changed_surface', 'binding_exists'])
def test_surface_freeze_refuses_changed_material_or_post_start_pin_mutation(natural, change):
    lab = surface_lab(natural)
    if change == 'different_material':
        lab.pins['candidate_image_id'] = 'sha256:' + 'e' * 64
    elif change == 'changed_surface':
        save(natural.run / 'trusted/tool-surface.json', {'different': True})
    else:
        save(natural.run / 'trusted/worker-a-binding.json', {})
    with pytest.raises(RuntimeError, match='绑定任何 Worker 前'):
        lab.freeze_calibrated_surface_pins()
    assert json.loads((natural.run / 'trusted/pins.json').read_text()) == PINS


def resumed_evidence(natural, monkeypatch):
    binding = bind_b(natural, monkeypatch)
    audit = json.loads((natural.run / 'trusted/native-crash-dsh.json').read_text())
    repaired = audit['events'] + audit['expected_repair_events']
    marker = {'seq': len(repaired), 'time': 200, 'type': 'session/end-seed', 'data': {}}
    restored = repaired + [marker]
    durable = audit['durable_goal']
    resumed = {'seq': len(restored), 'time': 201, 'type': 'goal/change', 'data': {
        'kind': 'goal/change', 'version': 1, 'operation': 'resume',
        'goal': {key: value for key, value in {**durable, 'revision': 2}.items()
            if key not in {'roundsStarted', 'createdAt', 'updatedAt'}},
        'roundsStarted': durable['roundsStarted'], 'createdAt': durable['createdAt'], 'updatedAt': 201}}
    final = restored + [resumed]
    # 只在原始字节之后追加原生修复测试行与 B 事件；
    # 验收函数必须独立关联这些记录。
    with natural.physical.open('ab') as stream:
        stream.write(b''.join(json.dumps(row).encode() + b'\n' for row in final[len(audit['events']):]))
    directory = natural.run / 'attempts/worker-b/candidate'
    save(directory / 'session-before-followup.json', {'scope': SCOPE, 'session_action': 'resume',
        'header': {**{key: value for key, value in audit['header'].items() if key != 'type'}, 'delegationDepth': audit['header'].get('delegationDepth', 0)}, 'agent_status': 'idle', 'inbox': {'has_pending': False},
        'first_live_seq': len(repaired), 'next_seq': len(restored), 'events': restored})
    uncertainty = [{'native_call_id': row['data']['message']['source']['callId'], 'error': row['data']['error'],
        'repair_seq': row['seq'], 'source_event_seqs': row.get('sourceEventSeqs', [])}
        for row in audit['expected_repair_events'] if row['type'] == 'tool/result']
    save(directory / 'native-crash-resume.json', {'schema': 'harness-lab-dsh-crash-resume/1', 'scope': SCOPE,
        'chain_id': SCOPE['variant'], 'session_id': 'main', 'audit_sha256': binding['recovery']['audit_sha256'],
        'boundary_sha256': binding['recovery']['boundary_sha256'], 'pause_index_used': False,
        'new_prompt_submitted': False, 'new_followup_submitted': False,
        'original_artifact': {'scope': SCOPE, 'physical_sha256': audit['physical_sha256'], 'physical_bytes': audit['physical_bytes']},
        'restored_goal': {**durable, 'activation': 'disarmed'}, 'restored_events_sha256': f.digest(restored),
        'original_prefix_sha256': audit['events_sha256'], 'repair_events': audit['expected_repair_events'],
        'uncertainty': uncertainty})
    save(directory / 'raw-events.jsonl', {'method': 'session.event', 'params': {'sessionId': 'main', 'event': resumed}})
    save(directory / 'fault-events.jsonl', {'phase': 'native_original_goal_resume_returned', 'scope': SCOPE,
        'attempt_id': 'worker-b', 'original_goal_id': durable['id'], 'original_goal_revision': durable['revision'],
        'goal': {**f.fold_durable_goal(final), 'activation': 'armed'}})
    return directory


def test_original_goal_native_repair_is_verified_but_does_not_imply_business_completion(natural, monkeypatch):
    resumed_evidence(natural, monkeypatch)
    evidence = f.deepseek_resume_evidence(natural, SCOPE, PINS)
    assert evidence['original_committed_bytes_preserved'] is True
    assert evidence['uncertainty'][0]['native_error']['code'] == 'TOOL_OUTCOME_UNKNOWN'
    assert evidence['native_goal_phase_at_end'] == 'active'
    assert evidence['business_automatic_completion_implied'] is False


@pytest.mark.parametrize('change', ['repaired_completed', 'original_byte', 'goal_replaced', 'uncertainty_hidden', 'activation_inferred', 'fake_resume_event', 'header_metadata', 'header_delegation'])
def test_self_projection_cannot_hide_repair_changes_or_replace_original_goal(natural, monkeypatch, change):
    directory = resumed_evidence(natural, monkeypatch)
    if change == 'original_byte':
        data = natural.physical.read_bytes().replace(b'"version": 1', b'"version": 2', 1)
        natural.physical.write_bytes(data)
    elif change in {'repaired_completed', 'goal_replaced', 'header_metadata', 'header_delegation'}:
        path = directory / 'session-before-followup.json'
        value = json.loads(path.read_text())
        if change == 'repaired_completed':
            value['events'][-2]['data']['reason'] = {'kind': 'completed'}
        elif change == 'goal_replaced':
            value['header']['id'] = 'replacement-session'
        elif change == 'header_metadata':
            value['header']['origin'] = 'replacement-metadata'
        else:
            value['header']['delegationDepth'] = 1
        save(path, value)
    elif change in {'uncertainty_hidden', 'activation_inferred'}:
        path = directory / 'native-crash-resume.json'
        value = json.loads(path.read_text())
        if change == 'uncertainty_hidden':
            value['uncertainty'] = []
        else:
            value['restored_goal']['activation'] = 'armed'
        save(path, value)
    else:
        path = directory / 'raw-events.jsonl'
        value = json.loads(path.read_text()); value['params']['event']['seq'] += 1; save(path, value)
    with pytest.raises(ValueError):
        f.deepseek_resume_evidence(natural, SCOPE, PINS)


@pytest.mark.parametrize('reason,capability_gap', [
    ('fault_dsh_original_session_not_persisted', True),
    ('fault_dsh_original_goal_not_persisted', True),
    ('fault_dsh_original_goal_not_resumable', True),
    ('fault_dsh_original_goal_objective_changed', False),
    ('fault_dsh_original_session_ambiguous', False),
    ('fault_dsh_original_profile_or_source_missing', False),
])
def test_missing_natural_work_is_a_gap_but_foreign_source_is_never_silently_accepted(natural, monkeypatch, reason, capability_gap):
    boundary = {'scope': SCOPE, 'chain_id': SCOPE['variant'], 'pause_index_used': False,
        'native_state_explicitly_flushed': False, 'natural_native_inventory': f.io.inventory(natural.state, natural.workspace),
        'model_budget': {'count': 2}}
    save(natural.run / 'trusted/crash-boundary.json', boundary)
    monkeypatch.setattr(f, 'model_snapshot', lambda *args: {'count': 2})
    def unavailable(*args):
        raise ValueError(reason)
    monkeypatch.setitem(f.NATIVE_CRASH_BINDERS, 'deepseek_harness', unavailable)
    args = SimpleNamespace(candidate='deepseek_harness', chain_id=SCOPE['variant'])
    if capability_gap:
        assert f.bind_crash_resume(args, natural, SCOPE, PINS) is False
        gap = json.loads((natural.run / 'trusted/native-recovery-gap.json').read_text())
        assert reason in gap['reason'] and gap['new_prompt_submitted'] is False
    else:
        with pytest.raises(ValueError, match=reason):
            f.bind_crash_resume(args, natural, SCOPE, PINS)
        assert not (natural.run / 'trusted/native-recovery-gap.json').exists()
    assert not (natural.fixture / 'attempts/worker-b/request.json').exists()
