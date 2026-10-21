"""I23 independent resource evidence calibration, not live Docker results."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from runner import fault_recovery as f


SCOPE = {'experiment_id': 'fault-recovery', 'variant': 'resources', 'user_id': 'alice', 'session_id': 'main'}
IMAGE = 'sha256:' + 'b' * 64


def snapshot(identity, name, *, running=True, image=IMAGE, memory=256 * 1024**2, cpu=250000000):
    return {'id': identity, 'name': '/' + name, 'image_id': image, 'memory_limit': memory, 'cpu_nano': cpu,
        'state': {'Status': 'running' if running else 'exited', 'Running': running, 'Pid': 10 if running else 0,
            'ExitCode': 0, 'OOMKilled': False, 'Dead': False, 'Paused': False, 'Restarting': False, 'Error': ''}}


def stamp(second):
    return f'2026-10-20T00:00:{second:02d}+00:00'


@pytest.fixture
def evidence(tmp_path):
    root = tmp_path / 'run'
    (root / 'trusted').mkdir(parents=True)
    paths = SimpleNamespace(run=root)
    worker_id, bench_id = 'a' * 64, 'e' * 64
    pins = {'chain_id': SCOPE['variant'], 'services_image_id': IMAGE, 'bench_image_id': 'sha256:' + 'f' * 64}
    worker = snapshot(worker_id, 'worker-a', image='sha256:' + 'c' * 64, memory=2 * 1024**3, cpu=10**9)
    processes = {'worker-a': {'before_start': worker}}
    services = {name: snapshot(str(index) * 64, 'harness-lab-' + name + '-1')
        for index, name in enumerate(('fixtures', 'tools', 'model-proxy'), 1)}
    bench = snapshot(bench_id, 'bench1', running=False, image=pins['bench_image_id'], memory=512 * 1024**2, cpu=500000000)
    f.create(root / 'trusted/bench-process-0001.json', {'exit_code': 0, 'after_exit': bench})
    f.create(root / 'trusted/service-host.json', services)
    f.create(root / 'trusted/orchestration-final.json', {'scope': SCOPE, 'chain_id': SCOPE['variant'], 'errors': [],
        'timed_out': False, 'used_active_seconds': 2.0, 'active_started_at': stamp(1), 'active_finished_at': stamp(3)})
    samples = []
    for second in (1, 3):
        rows, raw = [], []
        for item in [*services.values(), worker]:
            stats = {'Name': item['name'][1:], 'MemUsage': '8MiB / 2GiB', 'CPUPerc': '1.0%', 'PIDs': '2'}
            raw.append(stats)
            rows.append({**stats, 'id': item['id'], 'role': 'worker' if item is worker else 'shared_service',
                'memory_limit': item['memory_limit'], 'cpu_nano': item['cpu_nano']})
        samples.append({'at': stamp(second), 'error': None, 'containers': rows,
            'lifecycle': [], 'worker_ids': [worker_id], 'docker_stats_raw': '\n'.join(json.dumps(row) for row in raw)})
    return paths, pins, processes, samples


def save_samples(paths, samples):
    (paths.run / 'resource-samples.jsonl').write_text(''.join(json.dumps(row) + '\n' for row in samples))


def test_exact_raw_stats_and_lifecycle_produce_resource_result(evidence):
    paths, pins, processes, samples = evidence
    save_samples(paths, samples)
    result = f.resource_evidence(paths, SCOPE, pins, processes)
    assert result['passed'] is True and result['sample_count'] == 2
    assert result['kill_exit137_is_healthy_exit'] is False
    assert result['peaks']['allocated_memory_bytes'] == (2048 + 3 * 256) * 1024**2


@pytest.mark.parametrize('change', ['missing_stats', 'sampling_error', 'gap', 'unknown_id', 'reduced_cap', 'over_cpu', 'extra_worker', 'unknown_worker'])
def test_unknown_or_missing_measurements_never_become_zero_usage(evidence, change):
    paths, pins, processes, samples = evidence
    row = samples[0]
    if change == 'missing_stats':
        row['containers'].pop()
    elif change == 'sampling_error':
        row['error'] = 'missing stats'
    elif change == 'gap':
        samples[1]['at'] = stamp(20)
    elif change == 'unknown_id':
        row['containers'][-1]['id'] = 'f' * 64
    elif change == 'reduced_cap':
        row['containers'][-1]['memory_limit'] = 1024
    elif change == 'extra_worker':
        row['worker_ids'] += ['d' * 64, 'e' * 64]
    elif change == 'unknown_worker':
        row['worker_ids'] = ['d' * 64]
    else:
        row['containers'][-1]['CPUPerc'] = '301%'
        raw = [json.loads(item) for item in row['docker_stats_raw'].splitlines()]
        raw[-1]['CPUPerc'] = '301%'
        row['docker_stats_raw'] = '\n'.join(json.dumps(item) for item in raw)
    save_samples(paths, samples)
    with pytest.raises(ValueError):
        f.resource_evidence(paths, SCOPE, pins, processes)


@pytest.mark.parametrize('exit_code', [1, 137])
def test_missing_stats_cannot_hide_unhealthy_exit_or_injected_kill(evidence, exit_code):
    paths, pins, processes, samples = evidence
    before = processes['worker-a']['before_start']
    after = deepcopy(before)
    after['state'].update({'Status': 'exited', 'Running': False, 'Pid': 0, 'ExitCode': exit_code})
    samples[0]['lifecycle'] = [{'event': 'exited_during_sample', 'id': before['id'], 'before': before, 'after': after}]
    save_samples(paths, samples)
    with pytest.raises(ValueError, match='missing_stats_without_exact_healthy_exit'):
        f.resource_evidence(paths, SCOPE, pins, processes)


@pytest.mark.parametrize('change', [None, 'changed_sources', 'wrong_attempt', 'oom', 'gap', 'wrong_exit', 'boolean_attach'])
def test_controlled_exit1_still_rechecks_sources_processes_and_resource_limits(evidence, monkeypatch, change):
    paths, pins, processes, samples = evidence
    paths.fixture, paths.state = paths.run.parent / 'fixture', paths.run.parent / 'state'
    pins['candidate'] = 'deepseek_harness'
    before = processes['worker-a']['before_start']
    after = deepcopy(before)
    after['state'].update(Status='exited', Running=False, Pid=0, ExitCode=1)
    facts = {'kind': 'controlled_failure', 'result_sha256': 'a' * 64}
    processes['worker-a'].update(after_exit=after, process_exit=1, attach_exit=1, controlled_failure=facts,
        controlled_failure_native={'physical_native_failure_verified': True, 'reader': 'bound_public_dsh_jsonl'})
    event = {'event': 'controlled_failure_during_sample', 'id': before['id'], 'before': before,
        'after': after, 'attempt_id': 'worker-a', 'controlled_failure': facts}
    samples[1]['lifecycle'] = [event]
    samples[1]['containers'].pop()
    samples[1]['worker_ids'] = []
    samples[1]['docker_stats_raw'] = '\n'.join(json.dumps(row) for row in
        [json.loads(line) for line in samples[1]['docker_stats_raw'].splitlines()][:-1])
    calls = []
    def recheck(run, fixture, scope, frozen, candidate, attempt, actual, *, state):
        calls.append((candidate, attempt, actual, state))
        return {**facts, 'result_sha256': 'b' * 64} if change == 'changed_sources' else facts
    monkeypatch.setattr(f, 'controlled_failure', recheck)
    if change == 'wrong_attempt': event['attempt_id'] = 'worker-b'
    elif change == 'oom': after['state']['OOMKilled'] = True
    elif change == 'gap': samples[1]['at'] = stamp(20)
    elif change == 'wrong_exit': processes['worker-a']['attach_exit'] = 137
    elif change == 'boolean_attach': processes['worker-a']['attach_exit'] = True
    save_samples(paths, samples)
    if change is not None:
        with pytest.raises(ValueError):
            f.resource_evidence(paths, SCOPE, pins, processes)
        return
    result = f.resource_evidence(paths, SCOPE, pins, processes)
    assert result['passed'] is True and result['kill_exit137_is_healthy_exit'] is False
    assert calls == [('deepseek_harness', 'worker-a', after, paths.state)] * 2


@pytest.mark.parametrize('change', [None, 'wrong_action', 'other_exit', 'changed_scope', 'changed_boundary',
    'wrong_image', 'prompt_submitted', 'business_executed', 'b_started', 'b_bound', 'missing_gap',
    'transition_wrong_name', 'transition_wrong_hash', 'transition_wrong_action', 'transition_ordinary_exit',
    'missing_registration', 'changed_registration_boundary', 'foreign_registered_id', 'registered_running', 'b_created',
    'transition_not_running', 'dangling_b_created', 'control_dead', 'control_error', 'control_pid'])
def test_only_bound_native_gap_is_an_expected_control_diagnostic(evidence, change):
    paths, pins, processes, samples = evidence
    pins.update(candidate='deepseek_harness', candidate_image_id='sha256:' + 'c' * 64)
    boundary = {'scope': SCOPE, 'chain_id': SCOPE['variant'],
        'pause_index_used': False, 'native_state_explicitly_flushed': False}
    f.create(paths.run / 'trusted/crash-boundary.json', boundary)
    gap = {'schema': 'harness-lab-native-fault-gap/1', 'status': 'not_implemented',
        'scope': SCOPE, 'chain_id': SCOPE['variant'], 'candidate': 'deepseek_harness',
        'candidate_image_id': pins['candidate_image_id'], 'new_prompt_submitted': False,
        'business_actions_performed_by_bench': False,
        'crash_boundary_sha256': f.sha(f.regular(paths.run / 'trusted/crash-boundary.json'))}
    path = paths.run / 'trusted/bench-process-0001.json'
    control = f.read(path)
    control.update(action='bind-crash-resume', exit_code=3, name='bench1')
    control['after_exit']['state']['ExitCode'] = 3
    created = deepcopy(control['after_exit'])
    created['state'].update(Status='created', Running=False, ExitCode=0)
    registration = {'name': 'bench1', 'id': created['id'], 'action': 'bind-crash-resume',
        'image_id': pins['bench_image_id'], 'scope': SCOPE, 'chain_id': SCOPE['variant'],
        'before_start': created, 'crash_boundary_sha256': gap['crash_boundary_sha256']}
    if change == 'changed_registration_boundary': registration['crash_boundary_sha256'] = '0' * 64
    if change == 'foreign_registered_id': registration['id'] = '0' * 64
    if change == 'registered_running': registration['before_start']['state'].update(Status='running', Running=True)
    if change != 'missing_registration': f.create(paths.run / 'trusted/bench-registration-0001.json', registration)
    if change == 'wrong_action': control['action'] = 'after-kill'
    if change == 'other_exit':
        control['exit_code'] = control['after_exit']['state']['ExitCode'] = 1
    if change == 'control_dead': control['after_exit']['state']['Dead'] = True
    if change == 'control_error': control['after_exit']['state']['Error'] = 'runtime error'
    if change == 'control_pid': control['after_exit']['state']['Pid'] = 42
    if change == 'changed_scope': gap['scope'] = {**SCOPE, 'user_id': 'foreign'}
    if change == 'changed_boundary': gap['crash_boundary_sha256'] = '0' * 64
    if change == 'wrong_image': gap['candidate_image_id'] = 'sha256:' + '0' * 64
    if change == 'prompt_submitted': gap['new_prompt_submitted'] = True
    if change == 'business_executed': gap['business_actions_performed_by_bench'] = True
    if change == 'b_started': processes['worker-b'] = processes['worker-a']
    if change == 'b_bound': f.create(paths.run / 'trusted/worker-b-binding.json', {})
    if change == 'b_created': f.create(paths.run / 'trusted/worker-b-created.json', {})
    if change == 'dangling_b_created': (paths.run / 'trusted/worker-b-created.json').symlink_to('missing.json')
    if change != 'missing_gap': f.create(paths.run / 'trusted/native-recovery-gap.json', gap)
    path.write_text(json.dumps(control))
    if change != 'missing_gap':
        after = control['after_exit']
        before = deepcopy(after)
        before['state'].update(Status='running', Running=True, Pid=10, ExitCode=0)
        transition = {'event': 'diagnostic_exit_during_sample', 'id': after['id'],
            'before': before, 'after': after, 'control_action': 'bind-crash-resume',
            'registered_name': after['name'][1:],
            'gap_sha256': f.sha(f.regular(paths.run / 'trusted/native-recovery-gap.json')),
            'crash_boundary_sha256': f.sha(f.regular(paths.run / 'trusted/crash-boundary.json'))}
        if change == 'transition_wrong_name': transition['registered_name'] = 'foreign'
        if change == 'transition_wrong_hash': transition['gap_sha256'] = '0' * 64
        if change == 'transition_wrong_action': transition['control_action'] = 'after-kill'
        if change == 'transition_ordinary_exit': transition['event'] = 'exited_during_sample'
        if change == 'transition_not_running': transition['before']['state']['Running'] = False
        samples[0]['lifecycle'] = [transition]
    save_samples(paths, samples)
    if change is None:
        assert f.resource_evidence(paths, SCOPE, pins, processes)['passed'] is True
    else:
        with pytest.raises((ValueError, FileNotFoundError)):
            f.resource_evidence(paths, SCOPE, pins, processes)
