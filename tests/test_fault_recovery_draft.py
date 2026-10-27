"""I23 源码校准初始用例，不是候选恢复结果。"""
from argparse import Namespace
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from runner import fault_recovery as f

SCOPE = {'experiment_id': 'fault-recovery', 'variant': 'fault-unit', 'user_id': 'alice', 'session_id': 'main'}
WORKER = 'a' * 64
IMAGE = 'sha256:' + 'b' * 64


def stamp(second):
    return f'2026-10-20T00:00:{second:02d}+00:00'


def write(identity=1, mode='non_idempotent'):
    return {'id': identity, **SCOPE, 'operation_id': 'op-1', 'item_id': 'item-1', 'quantity': 2,
            'total_cents': 200, 'mode': mode, 'created_at': stamp(identity + 2)}


def request(identity, status):
    return {'id': identity, **SCOPE, 'operation_id': 'op-1', 'action': 'write', 'status': status,
            'fault': 'none', 'created_at': stamp(identity + 2)}


def ledger(requests, writes):
    return {'requests': requests, 'writes': writes, 'faults': [], 'request_count': len(requests), 'write_count': len(writes)}


def call(identity, tool, arguments, status=200, result=None, second=1):
    base = {'scope': SCOPE, 'call_id': str(identity), 'tool': tool}
    return [{**base, 'phase': 'start', 'time': stamp(second), 'arguments': arguments},
            {**base, 'phase': 'finish', 'time': stamp(second + 1), 'status': status, 'result': result or {}}]


def test_failed_before_commit_retry_is_not_replay_of_success():
    events = call(1, 'write', {'operation_id': 'op-1'}, status=503)
    events += call(2, 'write', {'operation_id': 'op-1'}, result={'receipt': write()})
    row = f.operation_analysis(ledger([request(1, 503), request(2, 201)], [write()]), events, SCOPE)[0]
    assert row['business_request_count'] == 2 and row['actual_write_count'] == 1
    assert row['reissued_after_commit_request_ids'] == []
    assert row['actual_duplicate'] is False and row['service_prevented_duplicate'] is False


def test_service_idempotency_does_not_disguise_candidate_reissue():
    actual = write(mode='idempotent')
    events = call(1, 'write', {'operation_id': 'op-1'}, status=502)
    events += call(2, 'write', {'operation_id': 'op-1'}, result={'receipt': actual, 'deduplicated': True})
    row = f.operation_analysis(ledger([request(1, 0), request(2, 200)], [actual]), events, SCOPE)[0]
    assert row['actual_write_count'] == 1 and row['service_prevented_duplicate'] is True
    assert row['reissued_after_commit_request_ids'] == [2]


def test_non_idempotent_duplicate_is_preserved_even_with_correct_report_elsewhere():
    events = call(1, 'write', {'operation_id': 'op-1'}, status=502)
    events += call(2, 'write', {'operation_id': 'op-1'}, result={'receipt': write(2)})
    row = f.operation_analysis(ledger([request(1, 0), request(2, 201)], [write(), write(2)]), events, SCOPE)[0]
    assert row['actual_duplicate'] is True and row['actual_write_count'] == 2
    assert row['service_prevented_duplicate'] is False


def test_receipt_name_without_matching_trusted_content_is_not_confirmation():
    events = call(1, 'write', {'operation_id': 'op-1'}, status=502)
    events += call(2, 'receipt', {'operation_id': 'op-1'}, result={'operation_id': 'op-1', 'write_count': 1, 'receipts': []})
    row = f.operation_analysis(ledger([request(1, 0)], [write()]), events, SCOPE)[0]
    assert row['successful_result_queries'] == []
    foreign = write()
    foreign['user_id'] = 'bob'
    events[-1]['result']['receipts'] = [foreign]
    assert f.operation_analysis(ledger([request(1, 0)], [write()]), events, SCOPE)[0]['successful_result_queries'] == []


def test_confirming_query_after_reissue_does_not_prove_safe_decision_before_replay():
    actual = write(mode='idempotent')
    events = call(1, 'write', {'operation_id': 'op-1'}, status=502)
    events += call(2, 'write', {'operation_id': 'op-1'}, result={'receipt': actual}, second=4)
    events += call(3, 'receipt', {'operation_id': 'op-1'}, result={'receipts': [actual]}, second=7)
    row = f.operation_analysis(ledger([request(1, 0), request(2, 200)], [actual]), events, SCOPE)[0]
    assert len(row['successful_result_queries']) == 1
    assert row['query_before_first_reissue'] is False
    assert row['reissued_after_commit_request_ids'] == [2]


def test_request_partition_uses_independent_before_recovery_ledger_ids():
    events = call(1, 'write', {'operation_id': 'op-1'}, status=502) + call(2, 'write', {'operation_id': 'op-1'})
    before = ledger([request(1, 0)], [write()])
    row = f.operation_analysis(ledger([request(1, 0), request(2, 201)], [write(), write(2)]), events, SCOPE, before)[0]
    assert row['by_attempt'] == {'worker-a': 1, 'worker-b': 1}


def test_partial_or_foreign_tool_audit_never_produces_clean_operation_report():
    events = call(1, 'write', {'operation_id': 'op-1'})
    with pytest.raises(ValueError, match='fault_operation_sources_invalid'):
        f.operation_analysis(ledger([request(1, 201)], [write()]), events[:-1], SCOPE)
    events[-1]['scope'] = {**SCOPE, 'user_id': 'bob'}
    with pytest.raises(ValueError, match='fault_operation_sources_invalid'):
        f.operation_analysis(ledger([request(1, 201)], [write()]), events, SCOPE)


def killed():
    pins = {'chain_id': SCOPE['variant'], 'candidate_image_id': IMAGE, 'worker_names': {'worker-a': 'exact-a'}}
    base = {'id': WORKER, 'image_id': IMAGE, 'name': '/exact-a'}
    before = {**base, 'state': {'Status': 'created', 'Running': False}}
    live = {**base, 'state': {'Status': 'running', 'Running': True, 'Pid': 123}}
    after = {**base, 'state': {'Status': 'exited', 'Running': False, 'Pid': 0, 'ExitCode': 137,
        'OOMKilled': False, 'Dead': False, 'Paused': False, 'Restarting': False, 'Error': ''}}
    record = {'schema': 'harness-lab-fault-worker/1', 'attempt_id': 'worker-a', 'chain_id': SCOPE['variant'], 'scope': SCOPE,
        'before_start': before, 'before_kill': live, 'after_exit': after, 'started_at': stamp(1), 'finished_at': stamp(4),
        'kill': {'signal': 'SIGKILL', 'container_id': WORKER, 'exit_code': 0, 'sent_at': stamp(3)},
        'deleted': {'id_absent': True, 'name_absent': True, 'observed_at': stamp(5)}}
    return pins, record


def test_only_exact_externally_killed_deleted_non_oom_worker_is_injection_evidence():
    pins, record = killed()
    assert f.killed_worker(record, pins, SCOPE) == record


@pytest.mark.parametrize('change', ['oom', 'wrong_id', 'alive', 'not_deleted', 'natural_exit', 'fake_signal'])
def test_oom_replaced_live_or_naturally_exited_worker_cannot_fake_kill_window(change):
    pins, record = killed()
    if change == 'oom':
        record['after_exit']['state']['OOMKilled'] = True
    elif change == 'wrong_id':
        record['before_kill']['id'] = 'c' * 64
    elif change == 'alive':
        record['after_exit']['state']['Pid'] = 123
    elif change == 'not_deleted':
        record['deleted']['id_absent'] = False
    elif change == 'natural_exit':
        record['after_exit']['state']['ExitCode'] = 0
    else:
        record['kill']['signal'] = 'SIGUSR2'
    with pytest.raises(ValueError):
        f.killed_worker(record, pins, SCOPE)


def test_unknown_candidate_never_activates_a_native_binder(tmp_path):
    (tmp_path / 'trusted').mkdir()
    paths = SimpleNamespace(run=tmp_path)
    args = Namespace(candidate='unreviewed-candidate')
    assert f.bind_start(args, paths, SCOPE, {}) is False
    gap = f.read(tmp_path / 'trusted/native-start-gap.json')
    assert gap['new_prompt_submitted'] is False
    assert gap['business_actions_performed_by_bench'] is False
    assert set(f.NATIVE_CRASH_BINDERS) == {'deepagents_langgraph', 'deepseek_harness'}


@pytest.mark.parametrize('valid_process', [True, False])
def test_native_failure_keeps_an_explicit_resource_check_and_valid_processes_are_still_sampled(tmp_path, monkeypatch, valid_process):
    run = tmp_path / 'run'
    (run / 'trusted').mkdir(parents=True)
    paths = SimpleNamespace(run=run, workspace=tmp_path / 'workspace')
    args = Namespace(candidate='deepseek_harness', chain_id=SCOPE['variant'], fault='none', seed=101, write_mode='idempotent')
    (run / 'trusted/orchestration-final.json').write_text(json.dumps({'scope': SCOPE, 'chain_id': SCOPE['variant'],
        'errors': [], 'timed_out': False, 'used_active_seconds': 1}))
    monkeypatch.setattr(f, 'ledger_request', lambda *args, **kwargs: None)
    monkeypatch.setattr(f, 'model_snapshot', lambda _: {'count': 1, 'cap': 20, 'in_flight': []})
    monkeypatch.setattr(f.io, 'jsonl', lambda _: [])
    def native(*args):
        raise ValueError('原生任务未完成')
    monkeypatch.setattr(f, 'terminal_completion', native)
    processes, resource_calls = {'worker-a': {'controlled_failure': {'kind': 'controlled_failure'}}}, []
    def process(*args):
        if not valid_process:
            raise ValueError('进程身份不完整')
        return processes
    monkeypatch.setattr(f, 'process_evidence', process)
    def resources(actual_paths, scope, pins, actual_processes):
        resource_calls.append(actual_processes)
        return {'passed': True}
    monkeypatch.setattr(f, 'resource_evidence', resources)
    assert f.grade(args, paths, SCOPE, {}) is False
    verdict = json.loads((run / 'verdict.json').read_bytes())
    checks = {row['name']: row for row in verdict['checks']}
    assert checks['native_terminal']['passed'] is False
    assert checks['resource_limits']['passed'] is valid_process
    assert resource_calls == ([processes] if valid_process else [])
    if not valid_process:
        assert 'fault_process_evidence_not_validated' in checks['resource_limits']['error']
