"""I23 sampling diagnostics calibration; all clocks and Docker calls are fake."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest

from runner import fault_recovery as grader
from scripts import run_fault_recovery as host


SECRET = 'must-never-appear-in-sampling-diagnostics'
WORKER = 'a' * 64
CANDIDATE = 'sha256:' + 'b' * 64
SERVICES = 'sha256:' + 'c' * 64
SCOPE = {'experiment_id': 'fault-recovery', 'variant': 'sampling-timing',
    'user_id': 'alice', 'session_id': 'main'}


class Clock:
    value = 1.0
    epoch = datetime(2026, 10, 20, tzinfo=timezone.utc).timestamp()

    def advance(self, seconds):
        self.value += seconds

    def monotonic(self):
        return self.value

    def time(self):
        return self.epoch + self.value

    def now(self):
        return datetime.fromtimestamp(self.time(), timezone.utc).isoformat()


def snapshot(identity, name, image, memory, cpu):
    return {'id': identity, 'name': '/' + name, 'image_id': image, 'image_tag': image,
        'memory_limit': memory, 'cpu_nano': cpu, 'mounts': [],
        'state': {'Status': 'running', 'Running': True, 'Pid': 10, 'ExitCode': 0,
            'OOMKilled': False, 'Dead': False, 'Paused': False, 'Restarting': False, 'Error': ''}}


def inspect_reply(record):
    return {'Id': record['id'], 'Name': record['name'], 'Image': record['image_id'],
        'Config': {'Image': record['image_tag'], 'Env': [SECRET]}, 'State': record['state'],
        'HostConfig': {'Memory': record['memory_limit'], 'NanoCpus': record['cpu_nano']}, 'Mounts': []}


@pytest.fixture
def exercise(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(host.time, 'monotonic', clock.monotonic)
    monkeypatch.setattr(host.time, 'time', clock.time)
    monkeypatch.setattr(host, 'now', clock.now)

    def run(*, slow_stage=None, timeout_stage=None, lock_delay=0, write_delay=0, write_failure=False,
            timing_failure=None, report_thread=False):
        root = tmp_path / 'run'
        (root / 'trusted').mkdir(parents=True)
        services = {name: snapshot(str(i) * 64, 'harness-lab-' + name + '-1', SERVICES,
            256 * 1024**2, 250000000) for i, name in enumerate(('fixtures', 'tools', 'model-proxy'), 1)}
        worker = snapshot(WORKER, 'exact-worker', CANDIDATE, 2 * 1024**3, 10**9)
        records = [*services.values(), worker]
        stats = [{'Name': r['name'][1:], 'MemUsage': '8MiB / 2GiB', 'CPUPerc': '1.0%',
            'PIDs': '2'} for r in records]
        lab = object.__new__(host.FaultLab)
        lab.paths = SimpleNamespace(run=root)
        lab.candidate_image_ids = {CANDIDATE}
        lab.pins = {'worker_names': {'worker-a': 'exact-worker'},
            'bench_image_id': 'sha256:' + 'd' * 64, 'chain_id': SCOPE['variant'],
            'services_image_id': SERVICES}
        state = SimpleNamespace(iteration=0, commands=[], samples=[], diagnostic=[], raised=None)
        state.sample_write_error = OSError(SECRET)
        state.timing_write_error = RuntimeError(SECRET)

        class Lock:
            held = False
            entries = 0

            def __enter__(self):
                self.entries += 1
                if self.entries == 2:
                    clock.advance(lock_delay)
                self.held = True

            def __exit__(self, *args):
                self.held = False

        lock = lab.sampling_lock = Lock()

        def call(_self, command, *, capture=False, check=True, timeout=None):
            assert lock.held, 'Original Docker observations remain under the sampling lock'
            if command[:3] == ['docker', 'ps', '-aq']:
                state.iteration += 1
                stage = 'inventory_ps'
                output = '\n'.join(r['id'] for r in records)
            elif command[:2] == ['docker', 'inspect']:
                stage = 'inventory_batch_inspect'
                assert command[2:] == [r['id'] for r in records]
                output = json.dumps([inspect_reply(r) for r in records])
            elif command[:2] == ['docker', 'stats']:
                stage = 'docker_stats'
                assert command[2:] == ['--no-stream', '--format', '{{json .}}', *[r['id'] for r in records]]
                output = '\n'.join(json.dumps(r) for r in stats)
            else:
                assert command == ['docker', 'ps', '-q', '--no-trunc', '--filter',
                    'label=com.docker.compose.project=harness-lab', '--filter', 'label=org.harness-lab.role=worker']
                stage = 'post_stats_worker_ps'
                output = WORKER + '\n'
            state.commands.append((state.iteration, stage, capture, check, timeout))
            clock.advance(.01)
            if state.iteration == 1 and stage in (slow_stage, timeout_stage):
                clock.advance(5.2)
            if state.iteration == 1 and stage == timeout_stage:
                raise subprocess.TimeoutExpired([SECRET], timeout=5, output=SECRET, stderr=SECRET)
            return SimpleNamespace(stdout=output, stderr=SECRET, returncode=0)

        # FaultLab.call still runs, so production tracing surrounds the exact
        # inherited transport boundary without a real subprocess or Docker.
        monkeypatch.setattr(host.PauseLab, 'call', call)
        original_open = Path.open
        writes = 0

        class Writer:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def write(self, text):
                nonlocal writes
                assert lock.held
                result = self.stream.write(text)
                writes += 1
                if writes == 1:
                    clock.advance(write_delay)
                    if write_failure:
                        raise state.sample_write_error
                return result

            def __exit__(self, *args):
                return self.stream.__exit__(*args)

        class TimingWriter:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def write(self, text):
                if timing_failure == 'write':
                    raise state.timing_write_error
                return self.stream.write(text)

            def __exit__(self, *args):
                result = self.stream.__exit__(*args)
                if timing_failure == 'close':
                    raise state.timing_write_error
                return result

        def opened(path, mode='r', *args, **kwargs):
            timing = mode == 'a' and path.name == 'resource-sampling-timing.jsonl'
            if timing:
                assert not lock.held, 'Diagnostic I/O must not lengthen the original lock scope'
                if timing_failure == 'open':
                    raise state.timing_write_error
            stream = original_open(path, mode, *args, **kwargs)
            if mode == 'a' and path.name == 'resource-samples.jsonl':
                return Writer(stream)
            if timing:
                return TimingWriter(stream)
            return stream

        monkeypatch.setattr(Path, 'open', opened)

        class Event:
            count = 0

            def is_set(self):
                return self.count >= 2

            def wait(self, seconds):
                assert not lock.held
                clock.advance(seconds)
                self.count += 1

        try:
            target = lab.sample_with_reporting if report_thread else lab.sample
            target(Event(), state.samples, root)
        except Exception as error:
            state.raised = error
        timing_path = root / 'resource-sampling-timing.jsonl'
        if timing_path.exists():
            state.diagnostic = [json.loads(line) for line in timing_path.read_text().splitlines()]
        state.path, state.lab = root, lab
        state.raw = (root / 'resource-samples.jsonl').read_bytes()
        assert [json.loads(line) for line in state.raw.splitlines()] == state.samples

        def write(name, data):
            (root / 'trusted' / name).write_text(json.dumps(data))

        write('service-host.json', services)
        first, last = [datetime.fromisoformat(state.samples[i]['at']).timestamp() for i in (0, -1)]
        write('orchestration-final.json', {'scope': SCOPE, 'chain_id': SCOPE['variant'], 'errors': [],
            'timed_out': False, 'used_active_seconds': last - first,
            'active_started_at': state.samples[0]['at'], 'active_finished_at': state.samples[-1]['at']})
        state.grade = lambda: grader.resource_evidence(lab.paths, SCOPE, lab.pins, {'worker-a': {'before_start': worker}})
        state.gap = last - first
        return state

    return run


def test_timing_preserves_original_commands_samples_and_next_loop_link(exercise):
    result = exercise()
    assert result.raised is None
    assert result.grade()['passed'] is True
    assert result.gap == pytest.approx(2)
    stages = ['inventory_ps', 'inventory_batch_inspect', 'docker_stats', 'post_stats_worker_ps']
    assert [(i, name) for i, name, *_ in result.commands] == [(i, name) for i in (1, 2) for name in stages]
    assert all(capture is True and check is True and timeout == 5 for _, _, capture, check, timeout in result.commands)
    first, second = result.diagnostic
    assert [row['stage'] for row in first['commands']] == stages
    assert first['previous_sample_sequence'] is None
    assert second['previous_sample_sequence'] == first['sample_sequence'] == 1
    assert second['previous_sample_write_finished_monotonic'] == first['sample_write']['finished_monotonic']
    assert second['loop_started_monotonic'] >= first['sample_write']['finished_monotonic']
    assert first['sample_write']['finished_monotonic'] >= first['commands'][-1]['finished_monotonic']
    assert all(row['return_code'] == 0 and row['error_type'] is None for row in first['commands'])
    diagnostic = (result.path / 'resource-sampling-timing.jsonl').read_text()
    assert SECRET not in diagnostic and WORKER not in diagnostic and CANDIDATE not in diagnostic
    assert not any(word in diagnostic for word in ('stdout', 'stderr', 'argv', 'Config', 'Env'))
    for sample in result.samples:
        assert sample['unix_time'] == pytest.approx(datetime.fromisoformat(sample['at']).timestamp())
        assert 'timing' not in sample and sample['error'] is None
    outcome = {}
    joins = []
    result.lab.finish_sampling(SimpleNamespace(join=lambda **kwargs: joins.append(kwargs), is_alive=lambda: False), outcome)
    assert joins == [{'timeout': 10}] and outcome['sampling_failures'] == []


@pytest.mark.parametrize('stage', ['inventory_ps', 'inventory_batch_inspect', 'docker_stats', 'post_stats_worker_ps'])
def test_slow_command_is_located_without_forgiving_original_five_second_gap(exercise, stage):
    result = exercise(slow_stage=stage)
    assert result.raised is None and result.gap > 5
    intervals = result.diagnostic[0]['commands']
    slow = [row for row in intervals if row['finished_monotonic'] - row['started_monotonic'] > 5]
    assert [row['stage'] for row in slow] == [stage]
    assert all(row['return_code'] == 0 and row['error_type'] is None for row in intervals)
    with pytest.raises(ValueError, match='fault_resource_sampling_gap'):
        result.grade()


def test_timeout_retains_sample_error_real_gap_and_sanitized_diagnostic(exercise):
    result = exercise(timeout_stage='docker_stats')
    assert result.raised is None and result.gap > 5
    interval = result.diagnostic[0]['commands'][-1]
    assert interval['stage'] == 'docker_stats'
    assert interval['finished_monotonic'] - interval['started_monotonic'] > 5
    assert interval['return_code'] is None and interval['error_type'] == 'TimeoutExpired'
    assert result.diagnostic[0]['sampling_error_type'] == 'TimeoutExpired'
    assert result.samples[0]['error'] == 'Docker resource sampling failed'
    assert result.samples[0]['worker_ids'] is None
    assert SECRET not in (result.path / 'resource-sampling-timing.jsonl').read_text()
    with pytest.raises(ValueError, match='fault_resource_sample_error'):
        result.grade()


def test_lock_wait_is_separate_from_commands_and_does_not_hide_gap(exercise):
    result = exercise(lock_delay=5.2)
    assert result.raised is None and result.gap > 5
    second = result.diagnostic[1]
    assert second['lock_acquired_monotonic'] - second['loop_started_monotonic'] == pytest.approx(5.2)
    assert all(row['finished_monotonic'] - row['started_monotonic'] < 1 for row in second['commands'])
    with pytest.raises(ValueError, match='fault_resource_sampling_gap'):
        result.grade()


def test_original_file_write_completion_is_recorded_and_delayed_write_still_fails_gap(exercise):
    result = exercise(write_delay=5.2)
    assert result.raised is None and result.gap > 5
    first, second = result.diagnostic
    interval = first['sample_write']
    assert interval['finished_monotonic'] - interval['started_monotonic'] == pytest.approx(5.2)
    assert interval['error_type'] is None
    assert second['previous_sample_write_finished_monotonic'] == interval['finished_monotonic']
    assert second['loop_started_monotonic'] >= interval['finished_monotonic']
    with pytest.raises(ValueError, match='fault_resource_sampling_gap'):
        result.grade()


def test_original_file_write_failure_is_rethrown_without_leaking_exception_text(exercise):
    result = exercise(write_failure=True)
    assert result.raised is result.sample_write_error
    interval = result.diagnostic[0]['sample_write']
    assert interval['error_type'] == 'OSError' and interval['finished_monotonic'] is not None
    assert SECRET not in (result.path / 'resource-sampling-timing.jsonl').read_text()
    assert not hasattr(result.lab._fault_sampling_trace, 'current')


@pytest.mark.parametrize('failure', ['open', 'write', 'close'])
def test_diagnostic_file_failure_does_not_kill_sampling_but_main_chain_rejects_it(exercise, failure):
    result = exercise(timing_failure=failure, report_thread=True)
    assert result.raised is None and len(result.samples) == 2
    # Even valid original resource measurements cannot make the main chain
    # accept an incomplete diagnostic/transport lifecycle.
    assert result.grade()['passed'] is True
    outcome = {}
    with pytest.raises(RuntimeError, match='I23 sampler reported incomplete evidence'):
        result.lab.finish_sampling(SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: False), outcome)
    assert outcome['sampling_failures'] == [{'stage': 'timing_write', 'error_type': 'RuntimeError'}] * 2
    assert SECRET not in json.dumps(outcome)
    assert not hasattr(result.lab._fault_sampling_trace, 'current')


def test_diagnostic_failure_cannot_replace_original_sample_write_exception(exercise):
    result = exercise(write_failure=True, timing_failure='open')
    assert result.raised is result.sample_write_error
    assert result.lab.sampling_failures == [{'stage': 'timing_write', 'error_type': 'RuntimeError'}]
    assert SECRET not in json.dumps(result.lab.sampling_failures)


def test_thread_reports_original_sample_write_failure_and_join_forces_main_failure(exercise):
    result = exercise(write_failure=True, report_thread=True)
    assert result.raised is None and len(result.samples) == 1
    assert result.lab.sampling_failures == [{'stage': 'sampler_thread', 'error_type': 'OSError'}]
    assert result.diagnostic[0]['sample_write']['error_type'] == 'OSError'
    outcome = {}
    with pytest.raises(RuntimeError, match='I23 sampler reported incomplete evidence'):
        result.lab.finish_sampling(SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: False), outcome)
    assert outcome['sampling_failures'] == result.lab.sampling_failures
    assert SECRET not in json.dumps(outcome)


@pytest.mark.parametrize('failure,error_type', [(SystemExit(SECRET), 'SystemExit'),
    (KeyboardInterrupt(SECRET), 'KeyboardInterrupt'), (ValueError(SECRET), 'ValueError')])
def test_unexpected_thread_termination_is_explicitly_reported_without_exception_text(failure, error_type):
    lab = object.__new__(host.FaultLab)

    def fail(*args):
        raise failure

    lab.sample = fail
    lab.sample_with_reporting(None, [], None)
    outcome = {}
    with pytest.raises(RuntimeError, match='I23 sampler reported incomplete evidence'):
        lab.finish_sampling(SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: False), outcome)
    assert outcome['sampling_failures'] == [{'stage': 'sampler_thread', 'error_type': error_type}]
    assert SECRET not in json.dumps(outcome)


def test_unclosed_sampler_still_forces_main_chain_failure():
    lab = object.__new__(host.FaultLab)
    outcome = {}
    with pytest.raises(RuntimeError, match='I23 sampler did not close its evidence'):
        lab.finish_sampling(SimpleNamespace(join=lambda **kwargs: None, is_alive=lambda: True), outcome)
    assert outcome['sampling_failures'] == []


def test_other_threads_do_not_enter_the_samplers_transport_trace(monkeypatch):
    lab = object.__new__(host.FaultLab)
    lab._fault_sampling_trace = threading.local()
    trace = {'operation': 'inventory', 'commands': []}
    lab._fault_sampling_trace.current = trace
    replies, errors = [], []

    def call(_self, command, **kwargs):
        assert command == ['docker', 'inspect', SECRET]
        return SimpleNamespace(stdout=SECRET, stderr=SECRET, returncode=0)

    monkeypatch.setattr(host.PauseLab, 'call', call)

    def concurrent_control():
        try:
            replies.append(lab.call(['docker', 'inspect', SECRET], capture=True))
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=concurrent_control)
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive() and not errors
    assert len(replies) == 1 and replies[0].stdout == SECRET
    assert trace['commands'] == []
