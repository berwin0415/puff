"""I23 host transport contract calibration; every Docker/process call is fake."""
import hashlib
from copy import deepcopy
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import pytest

from scripts import run_fault_recovery as host


SCOPE = {'experiment_id': 'fault-recovery', 'variant': 'host-calibration', 'user_id': 'alice', 'session_id': 'main'}
IMAGE = 'sha256:' + 'a' * 64
IDENTITY = 'b' * 64


@pytest.fixture
def lab(tmp_path):
    fixture, run, budget = tmp_path / 'fixture', tmp_path / 'run', tmp_path / 'budget'
    config = fixture / 'attempts/worker-a'
    for path in (config, run / 'trusted', run / 'attempts/worker-a/candidate', budget, tmp_path / 'state', tmp_path / 'workspace'):
        path.mkdir(parents=True)
    files = {fixture / 'scope.json': json.dumps(SCOPE), fixture / 'scope.token': 'local-tool-token',
        fixture / 'candidate-request.json': json.dumps({'prompt': 'original'}),
        budget / 'run.token': 'same-model-token', config / 'request.json': json.dumps({'prompt': 'original'})}
    for path, text in files.items():
        path.write_text(text)
    value = object.__new__(host.FaultLab)
    value.paths = SimpleNamespace(fixture=fixture, run=run, budget=budget,
        state=tmp_path / 'state', workspace=tmp_path / 'workspace')
    value.scope, value.env, value.names, value.timed_out = SCOPE, {}, [], False
    value.pins = {'candidate_image_id': IMAGE, 'model_name': 'frozen-model', 'worker_names': {'worker-a': 'exact-a'}}
    value.active_tick, value.sampling_lock = time.monotonic(), threading.RLock()
    value.control_registry = {}
    return value


class Process:
    returncode = 0
    def poll(self):
        return 0
    def wait(self, timeout):
        return 0


@pytest.mark.parametrize('candidate', ['deepagents_langgraph', 'deepseek_harness'])
def test_fixed_image_entrypoint_and_candidate_controls_reach_exact_docker_create(lab, candidate, monkeypatch):
    lab.args = SimpleNamespace(candidate=candidate, fault='none', chain_id=SCOPE['variant'])
    controls = ['native-crash.json'] if candidate == 'deepagents_langgraph' else ['fault-goal.json', 'fault-state.json']
    entrypoint = '/opt/venv/bin/python' if candidate == 'deepagents_langgraph' else 'node'
    directory = lab.paths.fixture / 'attempts/worker-a'
    for name in controls:
        (directory / name).write_text('{}')
    binding = {'schema': 'harness-lab-fault-worker-binding/1', 'scope': SCOPE, 'chain_id': SCOPE['variant'],
        'attempt_id': 'worker-a', 'candidate_image_id': IMAGE, 'mode': 'native-start', 'native_control_files': controls,
        'model_token_sha256': hashlib.sha256(b'same-model-token').hexdigest(),
        'request_sha256': hashlib.sha256((directory / 'request.json').read_bytes()).hexdigest(),
        'entrypoint': entrypoint, 'argv': (['/opt/materials/harness-lab/fault_entrypoint.py'] if candidate == 'deepagents_langgraph'
            else ['--import', 'tsx/esm', '/opt/materials/harness-lab/fault-goal-entrypoint.ts']) + ['--request', '/config/request.json']}
    (lab.paths.run / 'trusted/worker-a-binding.json').write_text(json.dumps(binding))
    mounts = lab.explicit_mounts('worker-a', binding)
    calls = []
    def call(command, **kwargs):
        calls.append(command)
        assert command[:2] == ['docker', 'create']
        return SimpleNamespace(stdout=IDENTITY + '\n', returncode=0)
    lab.call = call
    count = 0
    def snapshot(identity):
        nonlocal count
        count += 1
        assert identity == IDENTITY
        return {'id': IDENTITY, 'name': '/exact-a', 'image_id': IMAGE, 'memory_limit': 2 * 1024**3, 'cpu_nano': 10**9,
            'state': {'Status': 'created' if count == 1 else 'exited', 'Running': False, 'ExitCode': 0},
            'mounts': [{'Source': str(source), 'Destination': dest, 'RW': not readonly} for source, dest, readonly in mounts]}
    lab.snapshot = snapshot
    monkeypatch.setattr(host.subprocess, 'Popen', lambda *args, **kwargs: Process())
    result = lab.worker('worker-a')
    command = calls[0]
    assert command[command.index('--entrypoint') + 1] == entrypoint
    assert 'HARNESS_I23_IMAGE_ID=' + IMAGE in command
    assert command[command.index('--entrypoint') + 2] == IMAGE
    assert result['after_exit']['id'] == IDENTITY
    destinations = {dest for _, dest, _ in mounts}
    assert {'/config/' + name for name in controls} <= destinations
    assert '/config' not in destinations


@pytest.mark.parametrize('filename', ['native-extra.json', '../trusted/pins.json', '/config/native-crash.json', 'lifecycle-pause.json'])
def test_native_control_mounts_cannot_expand_the_candidate_whitelist(lab, filename):
    lab.args = SimpleNamespace(candidate='deepagents_langgraph', fault='none')
    with pytest.raises(RuntimeError, match='Unexpected native control file'):
        lab.explicit_mounts('worker-a', {'native_control_files': [filename]})


def prepare_kill_worker(lab):
    lab.args = SimpleNamespace(candidate='deepagents_langgraph', fault='kill_before_write', chain_id=SCOPE['variant'])
    directory = lab.paths.fixture / 'attempts/worker-a'
    for filename in ('native-crash.json', 'prewrite.json', 'prewrite.token'):
        (directory / filename).write_text('{}')
    binding = {'schema': 'harness-lab-fault-worker-binding/1', 'scope': SCOPE, 'chain_id': SCOPE['variant'],
        'attempt_id': 'worker-a', 'candidate_image_id': IMAGE, 'mode': 'native-start',
        'native_control_files': ['native-crash.json'],
        'model_token_sha256': hashlib.sha256(b'same-model-token').hexdigest(),
        'request_sha256': hashlib.sha256((directory / 'request.json').read_bytes()).hexdigest(),
        'entrypoint': '/opt/venv/bin/python',
        'argv': ['/opt/materials/harness-lab/fault_entrypoint.py', '--request', '/config/request.json']}
    (lab.paths.run / 'trusted/worker-a-binding.json').write_text(json.dumps(binding))
    (lab.paths.run / 'trusted/gate-signal.json').write_text('{}')
    return lab.explicit_mounts('worker-a', binding)


@pytest.mark.parametrize('exit_state', ['valid', 'exit1', 'still_running', 'oom', 'missing_exit', 'wrong_exit_id',
    'rm_failed', 'id_still_present', 'wrong_absence_id', 'name_still_present', 'absence_error'])
def test_kill_and_removal_use_separate_locks_but_attach_wait_allows_sampling(lab, monkeypatch, exit_state):
    mounts = prepare_kill_worker(lab)

    class Lock:
        held = False
        entries = 0
        def __enter__(self):
            assert not self.held
            self.held = True
            self.entries += 1
        def __exit__(self, *args):
            self.held = False

    lock, calls = Lock(), []
    lab.sampling_lock = lock

    class Attached:
        returncode = None
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            assert not lock.held, 'Docker attach wait must permit service resource sampling'
            assert self.returncode == 137
            calls.append('wait')
            return self.returncode

    process, destroyed, snapshots = Attached(), False, 0
    def call(command, **kwargs):
        nonlocal destroyed
        if command[:2] == ['docker', 'create']:
            assert not lock.held
            return SimpleNamespace(stdout=IDENTITY + '\n', returncode=0)
        if command[:2] == ['docker', 'kill']:
            assert lock.held and command == ['docker', 'kill', '--signal', 'SIGKILL', IDENTITY]
            process.returncode = 137
            calls.append('kill')
        elif command[:2] == ['docker', 'rm']:
            assert lock.held and lock.entries == 2, 'Removal cannot interrupt the sampler inventory'
            assert command == ['docker', 'rm', IDENTITY]
            assert kwargs == {'capture': True, 'timeout': 10}
            calls.append('rm')
            if exit_state == 'rm_failed':
                raise host.subprocess.CalledProcessError(1, command, stderr='No such container: ' + IDENTITY)
            destroyed = True
        else:
            raise AssertionError(command)
        return SimpleNamespace(stdout='', returncode=0)

    def snapshot(identity):
        nonlocal snapshots
        assert identity in (IDENTITY, 'exact-a')
        if destroyed:
            assert lock.held and lock.entries == 2, 'Exact absence inspection shares the removal lock'
            if exit_state == 'absence_error':
                raise RuntimeError('Docker identity inspection failed; absence is unproven')
            if exit_state == 'id_still_present' and identity == IDENTITY:
                return {'id': IDENTITY}
            if exit_state == 'wrong_absence_id' and identity == IDENTITY:
                return {'id': 'e' * 64}
            if exit_state == 'name_still_present' and identity == 'exact-a':
                return {'id': 'e' * 64, 'name': '/exact-a'}
            return None
        snapshots += 1
        state = {'Status': 'created' if snapshots == 1 else 'running', 'Running': snapshots != 1,
            'Pid': 0 if snapshots == 1 else 12, 'ExitCode': 0, 'OOMKilled': False, 'Dead': False,
            'Paused': False, 'Restarting': False, 'Error': ''}
        if process.returncode is not None:
            assert lock.held, 'Exact stopped state must be observed before releasing the boundary lock'
            state.update(Status='exited', Running=False, Pid=0, ExitCode=137)
            if exit_state == 'exit1': state['ExitCode'] = 1
            elif exit_state == 'still_running': state.update(Status='running', Running=True, Pid=12)
            elif exit_state == 'oom': state['OOMKilled'] = True
            elif exit_state == 'missing_exit': return None
        return {'id': 'e' * 64 if process.returncode is not None and exit_state == 'wrong_exit_id' else IDENTITY,
            'name': '/exact-a', 'image_id': IMAGE,
            'memory_limit': 2 * 1024**3, 'cpu_nano': 10**9, 'state': state,
            'mounts': [{'Source': str(source), 'Destination': dest, 'RW': not readonly} for source, dest, readonly in mounts]}

    lab.call, lab.snapshot = call, snapshot
    lab.bench = lambda *args, **kwargs: SimpleNamespace(returncode=0)
    monkeypatch.setattr(host.subprocess, 'Popen', lambda *args, **kwargs: process)
    if exit_state in {'exit1', 'still_running', 'oom', 'missing_exit', 'wrong_exit_id'}:
        with pytest.raises(RuntimeError, match='independently exit137'):
            lab.worker('worker-a')
        assert calls == ['kill'] and not destroyed and not lock.held and lock.entries == 1
        return
    if exit_state == 'rm_failed':
        with pytest.raises(host.subprocess.CalledProcessError): lab.worker('worker-a')
        assert calls == ['kill', 'wait', 'rm'] and not destroyed and not lock.held
        assert not (lab.paths.run / 'trusted/worker-a.json').exists()
        return
    if exit_state in {'id_still_present', 'wrong_absence_id', 'name_still_present', 'absence_error'}:
        expected = 'absence is unproven' if exit_state == 'absence_error' else 'removal was not independently observed'
        with pytest.raises(RuntimeError, match=expected): lab.worker('worker-a')
        assert calls == ['kill', 'wait', 'rm'] and destroyed and not lock.held
        assert not (lab.paths.run / 'trusted/worker-a.json').exists()
        return
    result = lab.worker('worker-a')
    assert calls == ['kill', 'wait', 'rm', 'wait'] and not lock.held and lock.entries == 2
    assert result['after_exit']['state']['ExitCode'] == result['attach_exit'] == result['process_exit'] == 137
    assert result['deleted']['id_absent'] is True and result['deleted']['name_absent'] is True
    assert result['kill_observation_lock_seconds'] >= 0


def test_worker_removal_cannot_split_sampler_id_list_and_batch_inspection(lab, monkeypatch):
    mounts = prepare_kill_worker(lab)
    listed, removal_waiting, inspected = threading.Event(), threading.Event(), threading.Event()
    worker_thread = threading.get_ident()
    order, sampler_errors = [], []

    class Lock:
        def __init__(self):
            self.actual, self.owner, self.worker_entries = threading.RLock(), None, 0

        def __enter__(self):
            if threading.get_ident() == worker_thread:
                self.worker_entries += 1
                if self.worker_entries == 2:
                    removal_waiting.set()
            self.actual.acquire()
            self.owner = threading.get_ident()

        def __exit__(self, *args):
            self.owner = None
            self.actual.release()

    lock = lab.sampling_lock = Lock()
    destroyed, snapshots = False, 0

    def sample_inventory():
        try:
            with lock:
                records = lab.containers()
                assert [record['id'] for record in records] == [IDENTITY]
                inspected.set()
        except BaseException as error:
            sampler_errors.append(error)

    sampler = threading.Thread(target=sample_inventory)

    class Attached:
        returncode, waited = None, False

        def poll(self):
            return self.returncode

        def wait(self, timeout):
            assert lock.owner != worker_thread, 'Attach wait must release the kill boundary lock'
            if not self.waited:
                self.waited = True
                sampler.start()
                assert listed.wait(2), 'Sampler must list the retained exited Worker before removal'
            return self.returncode

    process = Attached()

    def call(command, **kwargs):
        nonlocal destroyed
        if command[:2] == ['docker', 'create']:
            return SimpleNamespace(stdout=IDENTITY + '\n', returncode=0)
        if command[:2] == ['docker', 'kill']:
            assert lock.owner == worker_thread and command == ['docker', 'kill', '--signal', 'SIGKILL', IDENTITY]
            process.returncode = 137
            return SimpleNamespace(stdout='', returncode=0)
        if command[:2] == ['docker', 'rm']:
            assert lock.owner == worker_thread and command == ['docker', 'rm', IDENTITY]
            assert inspected.is_set(), 'A must remain available through the batch inspection'
            order.append('rm'); destroyed = True
            return SimpleNamespace(stdout='', returncode=0)
        assert threading.get_ident() != worker_thread and lock.owner == threading.get_ident()
        if command[:3] == ['docker', 'ps', '-aq']:
            assert not destroyed
            order.append('ps'); listed.set()
            assert removal_waiting.wait(2), 'Removal must wait for this inventory lock'
            return SimpleNamespace(stdout=IDENTITY + '\n', returncode=0)
        assert command == ['docker', 'inspect', IDENTITY] and not destroyed
        order.append('inspect')
        raw = {'Id': IDENTITY, 'Name': '/exact-a', 'Image': IMAGE, 'Config': {'Image': IMAGE},
               'HostConfig': {'Memory': 2 * 1024**3, 'NanoCpus': 10**9}, 'Mounts': [],
               'State': {'Status': 'exited', 'Running': False, 'ExitCode': 137}}
        return SimpleNamespace(stdout=json.dumps([raw]), returncode=0)

    def snapshot(identity):
        nonlocal snapshots
        assert identity in (IDENTITY, 'exact-a')
        if destroyed:
            assert lock.owner == worker_thread
            return None
        snapshots += 1
        state = {'Status': 'created' if snapshots == 1 else 'running', 'Running': snapshots != 1,
                 'Pid': 0 if snapshots == 1 else 12, 'ExitCode': 0, 'OOMKilled': False, 'Dead': False,
                 'Paused': False, 'Restarting': False, 'Error': ''}
        if process.returncode is not None:
            state.update(Status='exited', Running=False, Pid=0, ExitCode=137)
        return {'id': IDENTITY, 'name': '/exact-a', 'image_id': IMAGE, 'memory_limit': 2 * 1024**3,
            'cpu_nano': 10**9, 'state': state,
            'mounts': [{'Source': str(source), 'Destination': dest, 'RW': not readonly} for source, dest, readonly in mounts]}

    lab.call, lab.snapshot = call, snapshot
    lab.bench = lambda *args, **kwargs: SimpleNamespace(returncode=0)
    monkeypatch.setattr(host.subprocess, 'Popen', lambda *args, **kwargs: process)
    try:
        result = lab.worker('worker-a')
    finally:
        removal_waiting.set()
        if sampler.ident is not None:
            sampler.join(timeout=3)
    assert not sampler.is_alive() and sampler_errors == []
    assert order == ['ps', 'inspect', 'rm'] and lock.worker_entries == 2
    assert result['deleted']['id_absent'] is True and result['deleted']['name_absent'] is True


@pytest.fixture
def diagnostic(lab):
    lab.args = SimpleNamespace(candidate='deepseek_harness', fault='kill_before_write', chain_id=SCOPE['variant'])
    lab.pins['bench_image_id'] = 'sha256:' + 'c' * 64
    name = 'harness-lab-fault-bench-' + SCOPE['variant'] + '-7'
    before = {'id': 'd' * 64, 'name': '/' + name, 'image_id': lab.pins['bench_image_id'],
        'memory_limit': 512 * 1024**2, 'cpu_nano': 500000000,
        'state': {'Status': 'running', 'Running': True, 'Pid': 12, 'ExitCode': 0,
            'OOMKilled': False, 'Dead': False, 'Paused': False, 'Restarting': False, 'Error': ''}}
    after = deepcopy(before)
    after['state'].update(Status='exited', Running=False, Pid=0, ExitCode=3)
    boundary = {'scope': SCOPE, 'chain_id': SCOPE['variant'],
        'pause_index_used': False, 'native_state_explicitly_flushed': False}
    boundary_path = lab.paths.run / 'trusted/crash-boundary.json'
    boundary_path.write_text(json.dumps(boundary))
    boundary_hash = hashlib.sha256(boundary_path.read_bytes()).hexdigest()
    gap = {'schema': 'harness-lab-native-fault-gap/1', 'status': 'not_implemented',
        'scope': SCOPE, 'chain_id': SCOPE['variant'], 'candidate': 'deepseek_harness',
        'candidate_image_id': IMAGE, 'new_prompt_submitted': False,
        'business_actions_performed_by_bench': False, 'crash_boundary_sha256': boundary_hash}
    (lab.paths.run / 'trusted/native-recovery-gap.json').write_text(json.dumps(gap))
    lab.control_registry[name] = {'name': name, 'id': before['id'], 'action': 'bind-crash-resume',
        'image_id': before['image_id'], 'scope': SCOPE, 'chain_id': SCOPE['variant'],
        'crash_boundary_sha256': boundary_hash}
    return lab, before, after


class OneSample:
    def __init__(self):
        self.stopped = False

    def is_set(self):
        return self.stopped

    def wait(self, duration):
        self.stopped = True


def take_missing_stats_sample(lab, before, after):
    lab.containers = lambda: [before]
    lab.snapshot = lambda identity: after
    lab.candidate_image_ids = {IMAGE}
    def call(command, **kwargs):
        assert command[:2] in (['docker', 'stats'], ['docker', 'ps'])
        return SimpleNamespace(stdout='', returncode=0)
    lab.call = call
    samples = []
    lab.sample(OneSample(), samples, lab.paths.run)
    return samples[0]


def test_registered_exit3_missing_stats_race_is_a_bound_diagnostic_not_healthy_zero(diagnostic):
    lab, before, after = diagnostic
    result = take_missing_stats_sample(lab, before, after)
    assert result['error'] is None and result['containers'] == [] and result['worker_ids'] == []
    assert len(result['lifecycle']) == 1
    transition = result['lifecycle'][0]
    assert transition['event'] == 'diagnostic_exit_during_sample'
    assert transition['control_action'] == 'bind-crash-resume'
    assert transition['registered_name'] == before['name'][1:]
    assert transition['before'] == before and transition['after'] == after
    assert transition['after']['state']['ExitCode'] == 3
    assert transition['gap_sha256'] == hashlib.sha256((lab.paths.run / 'trusted/native-recovery-gap.json').read_bytes()).hexdigest()
    assert transition['crash_boundary_sha256'] == hashlib.sha256((lab.paths.run / 'trusted/crash-boundary.json').read_bytes()).hexdigest()


@pytest.mark.parametrize('change', [
    'unregistered', 'other_action', 'other_id', 'other_name', 'other_image', 'exit1', 'exit137', 'missing_object',
    'oom', 'dead', 'running', 'pid_nonzero', 'state_error', 'foreign_scope', 'foreign_chain', 'foreign_candidate',
    'foreign_candidate_image', 'changed_boundary', 'changed_boundary_and_gap', 'new_prompt', 'business_actions',
    'pause_index', 'explicit_flush', 'b_binding', 'b_created', 'b_started', 'missing_gap', 'gap_link',
])
def test_unbound_or_unhealthy_missing_stats_never_becomes_an_expected_diagnostic(diagnostic, change):
    lab, before, after = diagnostic
    trusted = lab.paths.run / 'trusted'
    gap_path, boundary_path = trusted / 'native-recovery-gap.json', trusted / 'crash-boundary.json'
    gap, boundary = json.loads(gap_path.read_text()), json.loads(boundary_path.read_text())
    registration = lab.control_registry[before['name'][1:]]
    if change == 'unregistered': lab.control_registry.clear()
    elif change == 'other_action': registration['action'] = 'after-kill'
    elif change == 'other_id': after['id'] = 'e' * 64
    elif change == 'other_name': after['name'] = '/another-bench'
    elif change == 'other_image': after['image_id'] = IMAGE
    elif change == 'exit1': after['state']['ExitCode'] = 1
    elif change == 'exit137': after['state']['ExitCode'] = 137
    elif change == 'missing_object': after = None
    elif change == 'oom': after['state']['OOMKilled'] = True
    elif change == 'dead': after['state']['Dead'] = True
    elif change == 'running': after['state']['Running'] = True
    elif change == 'pid_nonzero': after['state']['Pid'] = 12
    elif change == 'state_error': after['state']['Error'] = 'runtime failure'
    elif change == 'foreign_scope': gap['scope'] = {**SCOPE, 'user_id': 'foreign'}
    elif change == 'foreign_chain': gap['chain_id'] = 'foreign-chain'
    elif change == 'foreign_candidate': gap['candidate'] = 'deepagents_langgraph'
    elif change == 'foreign_candidate_image': gap['candidate_image_id'] = 'sha256:' + 'f' * 64
    elif change in {'changed_boundary', 'changed_boundary_and_gap'}:
        boundary['unexpected_change'] = True
        boundary_path.write_text(json.dumps(boundary))
        if change == 'changed_boundary_and_gap':
            gap['crash_boundary_sha256'] = hashlib.sha256(boundary_path.read_bytes()).hexdigest()
    elif change == 'new_prompt': gap['new_prompt_submitted'] = True
    elif change == 'business_actions': gap['business_actions_performed_by_bench'] = True
    elif change == 'pause_index': boundary['pause_index_used'] = True
    elif change == 'explicit_flush': boundary['native_state_explicitly_flushed'] = True
    elif change in {'b_binding', 'b_created', 'b_started'}:
        filename = {'b_binding': 'worker-b-binding.json', 'b_created': 'worker-b-created.json', 'b_started': 'worker-b.json'}[change]
        (trusted / filename).write_text('{}')
    if change in {'pause_index', 'explicit_flush'}:
        boundary_path.write_text(json.dumps(boundary))
        updated = hashlib.sha256(boundary_path.read_bytes()).hexdigest()
        gap['crash_boundary_sha256'] = registration['crash_boundary_sha256'] = updated
    gap_path.write_text(json.dumps(gap))
    if change == 'missing_gap': gap_path.unlink()
    elif change == 'gap_link':
        gap_path.rename(trusted / 'linked-gap.json')
        gap_path.symlink_to(trusted / 'linked-gap.json')
    result = take_missing_stats_sample(lab, before, after)
    assert result['error'] is not None and result['worker_ids'] is None
    assert not result['lifecycle']


def test_ordinary_zero_exit_retains_the_existing_healthy_transition(diagnostic):
    lab, before, after = diagnostic
    lab.control_registry.clear()
    after['state']['ExitCode'] = 0
    result = take_missing_stats_sample(lab, before, after)
    assert result['error'] is None
    assert result['lifecycle'][0]['event'] == 'exited_during_sample'
    assert result['lifecycle'][0]['after']['state']['ExitCode'] == 0


@pytest.mark.parametrize('change', [None, 'unbound_source', 'foreign_id', 'foreign_name', 'not_running'])
def test_worker_exit1_sample_requires_registered_identity_and_redecoded_sources(lab, monkeypatch, change):
    lab.args = SimpleNamespace(candidate='deepseek_harness', fault='none')
    before = {'id': IDENTITY, 'name': '/exact-a', 'image_id': IMAGE,
        'memory_limit': 2 * 1024**3, 'cpu_nano': 10**9,
        'state': {'Status': 'running', 'Running': True, 'Pid': 12}}
    after = deepcopy(before)
    after['state'].update(Status='exited', Running=False, Pid=0, ExitCode=1,
        OOMKilled=False, Dead=False, Paused=False, Restarting=False, Error='')
    facts, calls = {'kind': 'controlled_failure', 'result_sha256': 'f' * 64}, []
    def recheck(run, fixture, scope, pins, candidate, attempt, actual, *, state):
        calls.append((run, fixture, scope, pins, candidate, attempt, actual, state))
        if change == 'unbound_source':
            raise ValueError('unbound failure evidence')
        return facts
    monkeypatch.setattr(host, 'controlled_failure', recheck)
    if change == 'foreign_id': after['id'] = 'f' * 64
    elif change == 'foreign_name': before['name'] = after['name'] = '/foreign'
    elif change == 'not_running': before['state']['Running'] = False
    if change == 'not_running':
        with pytest.raises(RuntimeError):
            lab.failure_exit_transition(before, after)
        assert calls == []
        return
    result = take_missing_stats_sample(lab, before, after)
    if change is not None:
        assert result['error'] is not None and result['worker_ids'] is None
        assert not result['lifecycle']
        return
    assert result['error'] is None and result['worker_ids'] == []
    event = result['lifecycle'][0]
    assert event['event'] == 'controlled_failure_during_sample' and event['controlled_failure'] == facts
    assert calls == [(lab.paths.run, lab.paths.fixture, SCOPE, lab.pins, 'deepseek_harness',
                      'worker-a', after, lab.paths.state)]


def test_diagnostic_control_is_registered_by_actual_created_id_before_start(diagnostic):
    lab, before, after = diagnostic
    (lab.paths.run / 'trusted/native-recovery-gap.json').unlink()
    lab.control_registry.clear()
    lab.args.seed, lab.args.write_mode = 101, 'idempotent'
    lab.bench_sequence, lab.day = 0, '2026-10-20'
    lab.bench_command = lambda: ['docker', 'run', '--rm', lab.pins['bench_image_id']]
    created = deepcopy(before)
    created['state'].update(Status='created', Running=False, Pid=0)
    calls = []
    def call(command, **kwargs):
        calls.append(command)
        if command[:2] == ['docker', 'create']:
            assert not lab.control_registry
            return SimpleNamespace(stdout=created['id'] + '\n', stderr='', returncode=0)
        assert command == ['docker', 'start', '--attach', created['id']]
        name = 'harness-lab-fault-bench-' + SCOPE['variant'] + '-1'
        registered = lab.control_registry[name]
        assert registered['id'] == created['id'] and registered['action'] == 'bind-crash-resume'
        saved = json.loads((lab.paths.run / 'trusted/bench-registration-0001.json').read_text())
        assert saved == registered and saved['before_start']['state']['Status'] == 'created'
        return SimpleNamespace(stdout='{}\n', stderr='', returncode=3)
    def snapshot(identity):
        return created if identity == created['id'] else after
    name = 'harness-lab-fault-bench-' + SCOPE['variant'] + '-1'
    created['name'] = after['name'] = '/' + name
    lab.call, lab.snapshot = call, snapshot
    result = lab.bench('bind-crash-resume', check=False)
    assert result.returncode == 3
    assert [row[1] for row in calls] == ['create', 'start']
