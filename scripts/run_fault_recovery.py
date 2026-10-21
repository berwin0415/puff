"""I23 Docker transport; source written, Docker calibration still required.

DA uses a separate original-head crash entry. DSH uses the public session
repair and original durable Goal resume path. No I22 pause-index is synthesized.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
from pathlib import Path
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from runner.bench import identifier
from runner.fault_outcome import controlled_failure
from scripts.run_pause_recovery import PauseLab, now, save
from scripts.lab import CANDIDATES


FAULTS = ('none', 'model_timeout', 'fail_once', 'drop_receipt_once', 'kill_before_write')


class FaultLab(PauseLab):
    def call(self, args, *, capture=False, check=True, timeout=None):
        """Trace only this sampler thread; never serialize a command or reply."""
        local = getattr(self, '_fault_sampling_trace', None)
        trace = getattr(local, 'current', None)
        if trace is None:
            return super().call(args, capture=capture, check=check, timeout=timeout)
        operation = trace['operation']
        if operation == 'inventory' and args[:3] == ['docker', 'ps', '-aq']:
            stage = 'inventory_ps'
        elif operation == 'inventory' and args[:2] == ['docker', 'inspect']:
            stage = 'inventory_batch_inspect'
        elif operation == 'stats' and args[:2] == ['docker', 'stats']:
            stage = 'docker_stats'
        elif operation == 'missing_stats' and args[:2] == ['docker', 'inspect']:
            stage = 'missing_stats_inspect'
        elif operation == 'worker_inventory' and args[:2] == ['docker', 'ps']:
            stage = 'post_stats_worker_ps'
        else:
            stage = 'other_sampling_command'
        interval = {'stage': stage, 'started_monotonic': time.monotonic(),
            'finished_monotonic': None, 'return_code': None, 'error_type': None}
        trace['commands'].append(interval)
        try:
            result = super().call(args, capture=capture, check=check, timeout=timeout)
            if type(result.returncode) is int:
                interval['return_code'] = result.returncode
            return result
        except Exception as error:
            interval['error_type'] = self.sampling_error_type(error)
            if isinstance(error, subprocess.CalledProcessError) and type(error.returncode) is int:
                interval['return_code'] = error.returncode
            raise
        finally:
            interval['finished_monotonic'] = time.monotonic()

    @staticmethod
    def sampling_error_type(error):
        # Exception text, command, stdout and stderr can contain secrets.
        if isinstance(error, subprocess.TimeoutExpired):
            return 'TimeoutExpired'
        if isinstance(error, subprocess.CalledProcessError):
            return 'CalledProcessError'
        if isinstance(error, OSError):
            return 'OSError'
        if isinstance(error, RuntimeError):
            return 'RuntimeError'
        if isinstance(error, ValueError):
            return 'ValueError'
        if isinstance(error, TypeError):
            return 'TypeError'
        if isinstance(error, AssertionError):
            return 'AssertionError'
        if isinstance(error, SystemExit):
            return 'SystemExit'
        if isinstance(error, KeyboardInterrupt):
            return 'KeyboardInterrupt'
        return 'Exception'

    def record_sampling_failure(self, stage, error):
        if not hasattr(self, 'sampling_failures'):
            self.sampling_failures = []
        self.sampling_failures.append({'stage': stage, 'error_type': self.sampling_error_type(error)})

    def sample_with_reporting(self, event, samples, evidence):
        """A terminated thread is not evidence of a successful sampler."""
        try:
            self.sample(event, samples, evidence)
        except BaseException as error:
            self.record_sampling_failure('sampler_thread', error)

    def finish_sampling(self, sampler, outcome):
        sampler.join(timeout=10)
        outcome['sampling_failures'] = list(getattr(self, 'sampling_failures', []))
        if sampler.is_alive():
            raise RuntimeError('I23 sampler did not close its evidence')
        if outcome['sampling_failures']:
            raise RuntimeError('I23 sampler reported incomplete evidence')

    def failure_exit_transition(self, before, after):
        """One exact, source-bound exit1; never a generic failed process."""
        if (not isinstance(after, dict) or before.get('state', {}).get('Running') is not True
                or any(before.get(key) != after.get(key) for key in ('id', 'name', 'image_id'))):
            raise RuntimeError('Missing stats is not an exact controlled Worker failure')
        attempts = [attempt for attempt, name in self.pins['worker_names'].items()
            if before.get('name') == '/' + name]
        if len(attempts) != 1:
            raise RuntimeError('Controlled failure is not a registered Worker')
        attempt = attempts[0]
        facts = controlled_failure(self.paths.run, self.paths.fixture, self.scope, self.pins,
            self.args.candidate, attempt, after, state=self.paths.state)
        return {'event': 'controlled_failure_during_sample', 'id': before['id'], 'before': before,
            'after': after, 'attempt_id': attempt, 'controlled_failure': facts}

    def bench(self, action, *, check=True, timeout=30):
        self.bench_sequence += 1
        name = 'harness-lab-fault-bench-' + self.args.chain_id + '-' + str(self.bench_sequence)
        self.names.append(name)
        command = self.bench_command()
        command.remove('--rm')
        command[2:2] = ['--name', name]
        command += ['-m', 'runner.fault_recovery', action,
            '--candidate', self.args.candidate, '--chain-id', self.args.chain_id,
            '--seed', str(self.args.seed), '--date', self.day, '--fault', self.args.fault,
            '--write-mode', self.args.write_mode]
        if action == 'bind-crash-resume':
            # Register the actual created ID before it can run or exit. The
            # sampler must never infer an expected diagnostic from exit3 alone.
            tick = time.monotonic()
            boundary_hash = hashlib.sha256((self.paths.run / 'trusted/crash-boundary.json').read_bytes()).hexdigest()
            command[1] = 'create'
            created = self.call(command, capture=True, timeout=timeout).stdout.strip()
            before = self.snapshot(created)
            if (len(created) != 64 or any(letter not in '0123456789abcdef' for letter in created)
                    or not before or before.get('id') != created or before.get('name') != '/' + name
                    or before.get('image_id') != self.pins['bench_image_id']
                    or before.get('memory_limit') != 512 * 1024**2 or before.get('cpu_nano') != 500000000
                    or before.get('state', {}).get('Status') != 'created'
                    or before['state'].get('Running') is not False):
                raise RuntimeError('I23 diagnostic control creation identity mismatch')
            registration = {'name': name, 'id': created, 'action': action,
                'image_id': self.pins['bench_image_id'], 'scope': self.scope,
                'chain_id': self.args.chain_id, 'registered_at': now(), 'before_start': before,
                'crash_boundary_sha256': boundary_hash}
            save(self.paths.run / 'trusted' / f'bench-registration-{self.bench_sequence:04d}.json', registration)
            with self.sampling_lock:
                self.control_registry[name] = registration
            available = timeout - (time.monotonic() - tick)
            if available <= 0:
                raise TimeoutError('I23 diagnostic control creation exhausted its window')
            result = self.call(['docker', 'start', '--attach', created], capture=True, check=False, timeout=available)
        else:
            result = self.call(command, capture=True, check=False, timeout=timeout)
        destination = self.paths.run if self.paths.run.exists() else self.paths.fixture
        # Poll invocations get a new name; no prior control evidence is replaced.
        with (destination / f'bench-{self.bench_sequence:04d}-{action}.log').open('x') as stream:
            stream.write(result.stdout + result.stderr)
        trusted = self.paths.run / 'trusted'
        if trusted.is_dir():
            save(trusted / f'bench-process-{self.bench_sequence:04d}.json', {
                'name': name, 'action': action, 'after_exit': self.snapshot(name), 'exit_code': result.returncode})
        if check and result.returncode:
            raise RuntimeError('I23 independent bench rejected ' + action)
        return result

    def diagnostic_exit_transition(self, before, after):
        """Classify one missing-stats race; final bench independently rechecks."""
        name = before.get('name', '').removeprefix('/')
        registered = self.control_registry.get(name)
        state = after.get('state', {}) if isinstance(after, dict) else {}
        if (not registered or registered.get('action') != 'bind-crash-resume'
                or registered.get('name') != name or registered.get('scope') != self.scope
                or registered.get('chain_id') != self.args.chain_id
                or registered.get('image_id') != self.pins['bench_image_id']
                or before.get('id') != registered.get('id') or not isinstance(after, dict)
                or after.get('id') != registered['id']
                or before.get('name') != after.get('name') or before.get('name') != '/' + name
                or before.get('image_id') != after.get('image_id') or before.get('image_id') != registered['image_id']
                or before.get('state', {}).get('Running') is not True
                or state.get('Status') != 'exited' or type(state.get('Pid')) is not int or state['Pid'] != 0
                or type(state.get('ExitCode')) is not int or state['ExitCode'] != 3
                or any(state.get(key) is not False for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
                or state.get('Error') != ''):
            raise RuntimeError('Missing stats is not a registered I23 diagnostic exit')
        trusted = self.paths.run / 'trusted'
        for filename in ('worker-b-binding.json', 'worker-b-created.json', 'worker-b.json'):
            path = trusted / filename
            if path.exists() or path.is_symlink():
                raise RuntimeError('I23 diagnostic cannot follow Worker B binding or creation')
        def material(path):
            if (any(parent.is_symlink() for parent in (path, *path.parents))
                    or not path.is_file() or path.stat().st_size > 8 * 1024**2):
                raise RuntimeError('I23 diagnostic material must be a bounded regular file')
            data = path.read_bytes()
            if len(data) > 8 * 1024**2:
                raise RuntimeError('I23 diagnostic material exceeds its bound')
            return data, json.loads(data)
        gap_data, gap = material(trusted / 'native-recovery-gap.json')
        boundary_data, boundary = material(trusted / 'crash-boundary.json')
        boundary_hash = hashlib.sha256(boundary_data).hexdigest()
        if (gap.get('schema') != 'harness-lab-native-fault-gap/1' or gap.get('status') != 'not_implemented'
                or gap.get('scope') != self.scope or gap.get('chain_id') != self.args.chain_id
                or self.scope.get('variant') != self.args.chain_id
                or gap.get('candidate') != self.args.candidate
                or gap.get('candidate_image_id') != self.pins['candidate_image_id']
                or gap.get('crash_boundary_sha256') != boundary_hash
                or registered.get('crash_boundary_sha256') != boundary_hash
                or gap.get('new_prompt_submitted') is not False
                or gap.get('business_actions_performed_by_bench') is not False
                or boundary.get('scope') != self.scope or boundary.get('chain_id') != self.args.chain_id
                or boundary.get('pause_index_used') is not False
                or boundary.get('native_state_explicitly_flushed') is not False):
            raise RuntimeError('I23 diagnostic gap is not bound to the original crash boundary')
        return {'event': 'diagnostic_exit_during_sample', 'id': before['id'], 'before': before, 'after': after,
            'control_action': 'bind-crash-resume', 'registered_name': name,
            'gap_sha256': hashlib.sha256(gap_data).hexdigest(), 'crash_boundary_sha256': boundary_hash}

    def sample(self, event, samples, evidence):
        """I23-only override; I22 keeps its strict healthy-exit sampler."""
        self._fault_sampling_trace = threading.local()
        previous = None
        while not event.is_set():
            tick = time.monotonic()
            diagnostic = {'schema': 'harness-lab-fault-sampling-timing/1',
                'sample_sequence': len(samples) + 1, 'loop_started_monotonic': tick,
                'previous_sample_sequence': previous['sample_sequence'] if previous else None,
                'previous_sample_write_finished_monotonic': previous['sample_write']['finished_monotonic'] if previous else None,
                'lock_acquired_monotonic': None, 'commands': [], 'sampling_error_type': None,
                'sample_write': None}
            try:
                with self.sampling_lock:
                    diagnostic['lock_acquired_monotonic'] = time.monotonic()
                    trace = {'operation': 'inventory', 'commands': diagnostic['commands']}
                    self._fault_sampling_trace.current = trace
                    phase = getattr(self, 'sampling_phase', 'services_idle')
                    sample = {'unix_time': time.time(), 'at': now(), 'phase': phase, 'containers': [], 'lifecycle': [], 'error': None}
                    try:
                        records = [r for r in self.containers() if r['state']['Running']]
                        ids = [r['id'] for r in records]
                        trace['operation'] = 'stats'
                        stats = self.call(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *ids], capture=True, timeout=5).stdout if ids else ''
                        sample['docker_stats_raw'] = stats
                        stat_rows = [json.loads(line) for line in stats.splitlines()]
                        by_name = {row['Name']: row for row in stat_rows}
                        if len(by_name) != len(stat_rows) or set(by_name) - {r['name'].removeprefix('/') for r in records}:
                            raise RuntimeError('Docker stats contain duplicate or unretained containers')
                        active, exited_ids = [], set()
                        for record in records:
                            if record['name'].removeprefix('/') in by_name:
                                active.append(record)
                                continue
                            trace['operation'] = 'missing_stats'
                            after = self.snapshot(record['id'])
                            state = after['state'] if after else {}
                            healthy = (after is not None and after['id'] == record['id']
                                and after['image_id'] == record['image_id'] and after['name'] == record['name']
                                and state.get('Status') == 'exited' and type(state.get('Pid')) is int and state['Pid'] == 0
                                and type(state.get('ExitCode')) is int and state['ExitCode'] == 0
                                and all(state.get(key) is False for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
                                and state.get('Error') == '')
                            if healthy:
                                transition = {'event': 'exited_during_sample', 'id': record['id'], 'before': record, 'after': after}
                            elif type(state.get('ExitCode')) is int and state['ExitCode'] == 1:
                                transition = self.failure_exit_transition(record, after)
                            else:
                                transition = self.diagnostic_exit_transition(record, after)
                            sample['lifecycle'].append(transition)
                            exited_ids.add(record['id'])
                        for record in active:
                            name = record['name'].removeprefix('/')
                            role = 'worker' if record['image_id'] in self.candidate_image_ids or name in self.pins['worker_names'].values() else 'bench' if record['image_id'] == self.pins['bench_image_id'] else 'shared_service'
                            sample['containers'].append({**by_name[name], 'id': record['id'], 'role': role,
                                'memory_limit': record['memory_limit'], 'cpu_nano': record['cpu_nano']})
                        trace['operation'] = 'worker_inventory'
                        role_ids = self.call(['docker', 'ps', '-q', '--no-trunc', '--filter', 'label=com.docker.compose.project=harness-lab',
                            '--filter', 'label=org.harness-lab.role=worker'], capture=True, timeout=5).stdout.split()
                        if exited_ids & set(role_ids):
                            raise RuntimeError('A supposedly exited Worker became running during sampling')
                        sample['worker_ids'] = sorted(set(role_ids) | {r['id'] for r in active if r['image_id'] in self.candidate_image_ids
                            or r['name'].removeprefix('/') in self.pins['worker_names'].values()})
                    except Exception as error:
                        diagnostic['sampling_error_type'] = self.sampling_error_type(error)
                        sample['error'] = 'Docker resource sampling failed'
                        sample['worker_ids'] = None
                    samples.append(sample)
                    write_interval = {'stage': 'resource_sample_write', 'started_monotonic': time.monotonic(),
                        'finished_monotonic': None, 'return_code': None, 'error_type': None}
                    diagnostic['sample_write'] = write_interval
                    try:
                        with (evidence / 'resource-samples.jsonl').open('a') as stream:
                            stream.write(json.dumps(sample) + '\n')
                    except Exception as error:
                        write_interval['error_type'] = self.sampling_error_type(error)
                        raise
                    finally:
                        write_interval['finished_monotonic'] = time.monotonic()
            finally:
                if hasattr(self._fault_sampling_trace, 'current'):
                    del self._fault_sampling_trace.current
                # This append happens outside the original sampling lock. The
                # next row links its loop entry to this row's completed write;
                # neither original samples nor their timestamps are rewritten.
                try:
                    with (evidence / 'resource-sampling-timing.jsonl').open('a') as stream:
                        stream.write(json.dumps(diagnostic) + '\n')
                except BaseException as error:
                    # A new diagnostic write must not mask an original samples
                    # exception. The joined main chain checks this fixed flag.
                    self.record_sampling_failure('timing_write', error)
            previous = diagnostic
            event.wait(max(0, 2 - (time.monotonic() - tick)))

    def freeze_calibrated_surface_pins(self):
        path = self.paths.run / 'trusted/pins.json'
        before = json.loads(path.read_text())
        surface = hashlib.sha256((self.paths.run / 'trusted/tool-surface.json').read_bytes()).hexdigest()
        expected = {**before, 'tool_surface_sha256': surface}
        if (self.pins != expected or any((self.paths.run / 'trusted' / (attempt + '-binding.json')).exists()
                for attempt in ('worker-a', 'worker-b'))):
            raise RuntimeError('Tool surface must be frozen in unchanged chain pins before any Worker binding')
        save(path, self.pins)

    def calibrate_surface(self):
        super().calibrate_surface()
        self.freeze_calibrated_surface_pins()

    def identity(self, attempt):
        return {'schema': 'harness-lab-fault-worker/1', 'candidate': self.args.candidate,
            'chain_id': self.args.chain_id, 'scope': self.scope, 'attempt_id': attempt}

    def remaining(self):
        # One continuous I23 active window includes controls and restart time.
        # It begins before A creation, ends only after the last Worker exits.
        return 300 - (time.monotonic() - self.active_tick)

    def explicit_mounts(self, attempt, binding):
        config = self.paths.fixture / 'attempts' / attempt
        mounts = [(config / 'request.json', '/config/request.json', True),
            (self.paths.fixture / 'scope.json', '/config/scope.json', True),
            (self.paths.fixture / 'scope.token', '/config/scope.token', True),
            (self.paths.budget / 'run.token', '/budget/run.token', True),
            (self.paths.state, '/state', False), (self.paths.workspace, '/workspace', False),
            (self.paths.run / 'attempts' / attempt / 'candidate', '/evidence', False)]
        # Reviewed native binders may add exact single-file control mounts from
        # this attempt only, never a whole config/state parent or trusted tree.
        for filename in binding.get('native_control_files', []):
            allowed = {'native-crash.json'} if self.args.candidate == 'deepagents_langgraph' else {'fault-goal.json', 'fault-state.json'}
            if filename not in allowed:
                raise RuntimeError('Unexpected native control file')
            mounts.append((config / filename, '/config/' + filename, True))
        if self.args.fault == 'kill_before_write':
            mounts += [(config / 'prewrite.json', '/config/prewrite.json', True),
                       (config / 'prewrite.token', '/config/prewrite.token', True)]
        if len({destination for _, destination, _ in mounts}) != len(mounts):
            raise RuntimeError('Duplicate explicit Worker destination')
        for source, _, _ in mounts:
            if not source.exists() or source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
                raise RuntimeError('Missing or linked Worker mount')
        return mounts

    def worker(self, attempt):
        binding_file = self.paths.run / 'trusted' / (attempt + '-binding.json')
        binding = json.loads(binding_file.read_text())
        if (binding.get('schema') != 'harness-lab-fault-worker-binding/1' or binding.get('scope') != self.scope
                or binding.get('chain_id') != self.args.chain_id or binding.get('attempt_id') != attempt
                or binding.get('candidate_image_id') != self.pins['candidate_image_id']
                or binding.get('model_token_sha256') != hashlib.sha256((self.paths.budget / 'run.token').read_bytes().strip()).hexdigest()
                or binding.get('mode') != ('native-start' if attempt == 'worker-a' else 'native-crash-resume')):
            raise RuntimeError('Worker native binding identity mismatch')
        request_file = self.paths.fixture / 'attempts' / attempt / 'request.json'
        if hashlib.sha256(request_file.read_bytes()).hexdigest() != binding.get('request_sha256'):
            raise RuntimeError('Worker request changed after independent native binding')
        request = json.loads(request_file.read_text())
        original = json.loads((self.paths.fixture / 'candidate-request.json').read_text())
        if attempt == 'worker-a' and request.get('prompt') != original['prompt']:
            raise RuntimeError('Original objective changed before first submission')
        if attempt == 'worker-b' and any(key in request for key in ('prompt', 'followup')):
            raise RuntimeError('Crash resume must not resubmit prompt or followup')
        argv = binding.get('argv')
        if (not isinstance(argv, list) or not argv or any(not isinstance(word, str) for word in argv)
                or argv[-2:] != ['--request', '/config/request.json']
                or binding.get('entrypoint') not in {'/opt/venv/bin/python', 'node'}):
            raise RuntimeError('Reviewed image-native entrypoint binding required')
        name = self.pins['worker_names'][attempt]
        mounts = self.explicit_mounts(attempt, binding)
        command = ['docker', 'create', '--name', name, '--platform', 'linux/arm64',
            '--label', 'com.docker.compose.project=harness-lab', '--label', 'org.harness-lab.role=worker',
            '--label', 'org.harness-lab.chain=' + self.args.chain_id,
            '--network', 'harness-lab_lab', '--read-only', '--memory', '2g', '--cpus', '1',
            '--pids-limit', '256', '--tmpfs', '/tmp:size=128m',
            '-e', 'MODEL_API_BASE_URL=http://model-proxy:8082/v1', '-e', 'MODEL_NAME=' + self.pins['model_name'],
            '-e', 'MODEL_RUN_TOKEN_FILE=/budget/run.token', '-e', 'HARNESS_I23_IMAGE_ID=' + self.pins['candidate_image_id'],
            '-e', 'HARNESS_LIFECYCLE_IMAGE_ID=' + self.pins['candidate_image_id']]
        if self.args.fault == 'kill_before_write':
            command += ['-e', 'HARNESS_I23_PREWRITE_BINDING_FILE=/config/prewrite.json']
        for source, destination, readonly in mounts:
            command += ['--mount', f'type=bind,src={source},dst={destination}' + (',readonly' if readonly else '')]
        command += ['--entrypoint', binding['entrypoint'], self.pins['candidate_image_id'], *argv]
        self.names.append(name)  # even a timed-out create may leave this object
        record = {**self.identity(attempt), 'started_at': now(), 'kill': None, 'deleted': None,
                  'errors': [], 'binding_sha256': hashlib.sha256(binding_file.read_bytes()).hexdigest()}
        identifier_ = self.call(command, capture=True, timeout=max(0.1, min(30, self.remaining()))).stdout.strip()
        before = self.snapshot(identifier_)
        if (not before or before['id'] != identifier_ or before['name'] != '/' + name
                or before['image_id'] != self.pins['candidate_image_id'] or before['state'].get('Status') != 'created'
                or before['state'].get('Running') is not False or before['memory_limit'] != 2 * 1024**3 or before['cpu_nano'] != 10**9):
            raise RuntimeError('Unexpected created Worker identity')
        by_dest = {row['Destination']: row for row in before['mounts']}
        if (len(by_dest) != len(before['mounts']) or set(by_dest) != {dest for _, dest, _ in mounts}
                or any(by_dest[dest]['Source'] != str(source) or by_dest[dest]['RW'] is readonly
                       for source, dest, readonly in mounts)):
            raise RuntimeError('Unexpected created Worker mount')
        record['before_start'] = before
        save(self.paths.run / 'trusted' / (attempt + '-created.json'), record)
        self.sampling_phase = attempt + '_active'
        if self.remaining() <= 0:
            raise TimeoutError('I23 cumulative active window exhausted before Worker start')
        process = None
        try:
            with (self.paths.run / 'attempts' / attempt / 'candidate.log').open('x') as stream:
                process = subprocess.Popen(['docker', 'start', '--attach', identifier_], cwd=ROOT,
                    env=self.env, text=True, stdout=stream, stderr=subprocess.STDOUT)
                while process.poll() is None:
                    if self.remaining() <= 0:
                        self.timed_out = True
                        self.call(['docker', 'kill', identifier_], capture=True, check=False, timeout=10)
                        raise TimeoutError('I23 cumulative active window exhausted')
                    if attempt == 'worker-a' and self.args.fault == 'kill_before_write':
                        observed = self.bench('observe-gate', check=False, timeout=max(0.1, min(10, self.remaining())))
                        if observed.returncode != 0:
                            raise RuntimeError('Trusted prewrite observation failed')
                        if (self.paths.run / 'trusted/gate-signal.json').is_file():
                            signal_file = self.paths.run / 'trusted/gate-signal.json'
                            # Exclude the running -> injected137 boundary from
                            # concurrent stats. Attach waits stay outside the
                            # lock; deletion gets its own inventory-safe lock.
                            with self.sampling_lock:
                                lock_tick = time.monotonic()
                                live = self.snapshot(identifier_)
                                if (not live or live['id'] != identifier_ or live['image_id'] != self.pins['candidate_image_id']
                                        or live['state'].get('Running') is not True or live['state'].get('OOMKilled') is not False):
                                    raise RuntimeError('Exact Worker not independently live at gate hit')
                                record['before_kill'] = live
                                record['kill'] = {'signal': 'SIGKILL', 'container_id': identifier_, 'sent_at': now(),
                                    'gate_signal_sha256': hashlib.sha256(signal_file.read_bytes()).hexdigest()}
                                result = self.call(['docker', 'kill', '--signal', 'SIGKILL', identifier_], capture=True, check=False, timeout=10)
                                record['kill']['exit_code'] = result.returncode
                                if result.returncode:
                                    raise RuntimeError('Docker did not confirm exact-ID SIGKILL')
                                record['after_exit'] = self.snapshot(identifier_)
                                stopped = record['after_exit']
                                state = stopped.get('state', {}) if isinstance(stopped, dict) else {}
                                if (not stopped or stopped.get('id') != identifier_ or stopped.get('name') != '/' + name
                                        or stopped.get('image_id') != self.pins['candidate_image_id']
                                        or state.get('Status') != 'exited' or type(state.get('Pid')) is not int or state['Pid'] != 0
                                        or type(state.get('ExitCode')) is not int or state['ExitCode'] != 137
                                        or any(state.get(key) is not False for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
                                        or state.get('Error') != ''):
                                    raise RuntimeError('Exact Worker did not independently exit137 after SIGKILL')
                                record['finished_at'] = now()
                                record['kill_observation_lock_seconds'] = time.monotonic() - lock_tick
                            process.wait(timeout=max(0.1, min(10, self.remaining())))
                            # containers() lists IDs before inspecting them.
                            # Never remove A between those two observations.
                            with self.sampling_lock:
                                self.call(['docker', 'rm', identifier_], capture=True, timeout=10)
                                record['deleted'] = {'id_absent': self.snapshot(identifier_) is None,
                                    'name_absent': self.snapshot(name) is None, 'observed_at': now()}
                                if not record['deleted']['id_absent'] or not record['deleted']['name_absent']:
                                    raise RuntimeError('Exact killed Worker removal was not independently observed')
                            break
                    time.sleep(0.25)
                process.wait(timeout=max(0.1, min(10, self.remaining())))
                record['attach_exit'] = process.returncode
        finally:
            # Kill service work before any subsequent evidence I/O failure can
            # strand paid execution. Docker rm is retained for generic cleanup.
            if process is not None and process.poll() is None:
                self.call(['docker', 'kill', identifier_], capture=True, check=False, timeout=10)
                process.terminate()
                process.wait(timeout=10)
        after = record.get('after_exit') or self.snapshot(identifier_)
        record.update({'after_exit': after, 'finished_at': record.get('finished_at', now()), 'timed_out': self.timed_out,
                       'process_exit': after['state'].get('ExitCode') if after else None})
        save(self.paths.run / 'trusted' / (attempt + '.json'), record)
        return record

    def run_chain(self, args):
        self.args = args
        if self.containers():
            raise RuntimeError('Existing experiment containers must be cleaned first')
        self.config.initialize_layout()
        self.paths = self.config.paths_for('fault-recovery', args.chain_id, args.candidate, 'alice', 'main', args.chain_id, case='fault-recovery')
        if any(path.exists() or path.is_symlink() for path in vars(self.paths).values()):
            raise RuntimeError('Fault chain ID already used')
        self.day = self.paths.run.parent.parent.parent.name
        self.scope = {'experiment_id': 'fault-recovery', 'variant': args.chain_id, 'user_id': 'alice', 'session_id': 'main'}
        self.service = CANDIDATES[args.candidate][0]
        self.env.update({'LAB_EXPERIMENT_ID': 'fault-recovery', 'LAB_VARIANT': args.chain_id,
            'LAB_EVIDENCE_HOST': str(self.paths.run), 'LAB_WORKSPACE_HOST': str(self.paths.workspace),
            'LAB_STATE_HOST': str(self.paths.state), 'LAB_BUDGET_HOST': str(self.paths.budget),
            'LAB_FIXTURE_HOST': str(self.paths.fixture), 'LAB_MAX_MODEL_CALLS': '20'})
        resolved = json.loads(self.compose('--profile', 'model', '--profile', self.service, 'config', '--format', 'json', capture=True).stdout)
        model = resolved['services']['model-proxy']['environment'].get('MODEL_NAME')
        del resolved
        if not isinstance(model, str) or not model:
            raise RuntimeError('Selected remote model name missing')
        self.env['LAB_MODEL_NAME'] = model
        context = args.materials_context.resolve()
        if 'old' in context.parts or not context.is_relative_to(ROOT / 'external/build-context'):
            raise RuntimeError('I23 material context must be current and separate')
        source = json.loads((context / 'SOURCE_MANIFEST.json').read_text())
        adapter = json.loads((context / 'LAB_ADAPTER_MANIFEST.json').read_text())
        overlay = json.loads((context / 'LAB_FAULT_BRIDGE_MANIFEST.json').read_text())
        image_id = self.call(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.image_tag], capture=True).stdout.strip()
        bench_id = self.call(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.bench_image], capture=True).stdout.strip()
        services_id = self.call(['docker', 'image', 'inspect', '--format', '{{.Id}}', args.services_image], capture=True).stdout.strip()
        self.pins = {'schema': 'harness-lab-fault-pins/1', 'candidate': args.candidate, 'chain_id': args.chain_id,
            'scope': self.scope, 'seed': args.seed, 'date': self.day, 'fault': args.fault, 'write_mode': args.write_mode,
            'candidate_image_id': image_id, 'bench_image_id': bench_id, 'model_name': model,
            'source_export_sha256': source['export_sha256'], 'adapter_export_sha256': adapter['export_sha256'],
            'fault_overlay_sha256': hashlib.sha256((context / 'LAB_FAULT_BRIDGE_MANIFEST.json').read_bytes()).hexdigest(),
            'worker_names': {attempt: 'harness-lab-fault-' + args.chain_id + '-' + attempt for attempt in ('worker-a', 'worker-b')},
            'model_call_cap': 20, 'active_limit_seconds': 300}
        self.pins['adapter_helper_sha256'] = hashlib.sha256((ROOT / 'adapters/deepagents_langgraph/pause_recovery.py').read_bytes()).hexdigest()
        self.pins['services_image_id'] = services_id
        self.candidate_image_ids = {image_id}
        self.paths.fixture.mkdir(parents=True, exist_ok=False)
        save(self.paths.fixture / 'host-pins.json', self.pins)
        # The real upstream credential remains inherited only by model-proxy.
        # Neither expanded compose nor Config.Env is written to evidence.
        services = {name: {'image': services_id} for name in ('fixtures', 'tools', 'model-proxy')}
        services['model-proxy']['command'] = ['-m', 'execution.model_proxy', '--database', '/budget/calls.sqlite',
            '--run-token-file', '/budget/run.token', '--cap', '20', '--port', '8082',
            '--inject-timeout-at', '1' if args.fault == 'model_timeout' else '']
        if args.fault == 'kill_before_write':
            services['tools'].update({'command': ['-m', 'execution.tools.server', '--binding-file', '/config/binding.json',
                '--prewrite-binding-file', '/config/prewrite-service.json'], 'volumes': [
                {'type': 'bind', 'source': str(self.paths.fixture / 'prewrite-service.json'), 'target': '/config/prewrite-service.json', 'read_only': True},
                {'type': 'bind', 'source': str(self.paths.fixture / 'prewrite-private'), 'target': '/private'}]})
        override = self.paths.fixture / 'fault-services.compose.json'
        save(override, {'services': services})
        self.prefix += ['-f', str(override)]
        self.names, self.bench_sequence, self.timed_out = [], 0, False
        self.control_registry = {}
        self.sampling_failures = []
        self.sampling_lock, samples, stop = threading.RLock(), [], threading.Event()
        self.samples, sampler, self.active_tick = samples, None, None
        outcome = {'schema': 'harness-lab-fault-orchestration/1', 'chain_id': args.chain_id,
            'scope': self.scope, 'started_at': now(), 'errors': [], 'timed_out': False}
        try:
            self.compose('up', '-d', '--no-build', 'fixtures')
            self.wait_health('fixtures', 8080)
            self.bench('prepare', timeout=90)
            self.compose('up', '-d', '--no-build', 'tools', 'model-proxy')
            self.wait_health('tools', 8081)
            self.wait_health('model-proxy', 8082)
            save(self.paths.run / 'trusted/service-host.json', {name: self.snapshot('harness-lab-' + name + '-1')
                for name in ('fixtures', 'tools', 'model-proxy')})
            self.bench('configure')
            if args.candidate == 'deepseek_harness':
                self.calibrate_surface()
            # Running a proxy makes no model request. No candidate is created
            # until an independently reviewed native start binding exists.
            binding = self.bench('bind-start', check=False)
            if binding.returncode == 3:
                outcome['native_start_available'] = False
                return 3
            if binding.returncode:
                raise RuntimeError('Native start binder rejected I23 variant')
            outcome['native_start_available'] = True
            sampler = threading.Thread(target=self.sample_with_reporting, args=(stop, samples, self.paths.run), daemon=True)
            sampler.start()
            outcome['active_started_at'] = now()
            self.active_tick = time.monotonic()
            a = self.worker('worker-a')
            if args.fault == 'kill_before_write' and a['kill'] is not None:
                # Time spent collecting controls/state and recreating Worker B
                # stays inside the same 300-second active window.
                self.bench('after-kill', timeout=max(0.1, min(20, self.remaining())))
                save(self.paths.run / 'trusted/resume-clock.json', {'chain_id': args.chain_id, 'scope': self.scope,
                    'used_active_seconds': time.monotonic() - self.active_tick,
                    'boundary_sha256': hashlib.sha256((self.paths.run / 'trusted/crash-boundary.json').read_bytes()).hexdigest()})
                bound = self.bench('bind-crash-resume', check=False, timeout=max(0.1, min(20, self.remaining())))
                if bound.returncode == 0:
                    self.worker('worker-b')
                elif bound.returncode != 3:
                    raise RuntimeError('Native crash resume binder rejected preserved state')
            self.used_active = time.monotonic() - self.active_tick
            outcome['active_finished_at'] = now()
            self.halt(services=('model-proxy',))
            stop.set()
            self.finish_sampling(sampler, outcome)
            outcome.update({'used_active_seconds': self.used_active, 'timed_out': self.timed_out, 'finished_at': now()})
            save(self.paths.run / 'trusted/orchestration-final.json', outcome)
            return self.bench('grade', check=False, timeout=60).returncode
        except Exception as error:
            outcome['errors'].append(type(error).__name__)
            raise
        finally:
            self.halt(names=self.names, services=('model-proxy',))
            stop.set()
            if sampler:
                sampler.join(timeout=10)
            outcome['sampling_failures'] = list(self.sampling_failures)
            try:
                if self.paths.run.exists():
                    for service in ('fixtures', 'tools', 'model-proxy'):
                        log = self.compose('logs', '--no-color', service, capture=True, check=False, timeout=10)
                        (self.paths.run / (service + '.log')).write_text(log.stdout + log.stderr)
                    save(self.paths.run / 'orchestration.json', {**outcome, 'finished_at': now(), 'timed_out': self.timed_out})
            finally:
                self.cleanup(('model-proxy', 'tools', 'fixtures'), names=self.names)
                destination = self.paths.run if self.paths.run.is_dir() else self.paths.fixture
                save(destination / 'cleanup.json', {'remaining': self.containers(), 'finished_at': now()})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', choices=('deepagents_langgraph', 'deepseek_harness'), required=True)
    parser.add_argument('--chain-id', type=identifier, required=True)
    parser.add_argument('--seed', type=int, choices=(101, 202, 303, 404, 505), required=True)
    parser.add_argument('--fault', choices=FAULTS, required=True)
    parser.add_argument('--write-mode', choices=('idempotent', 'non_idempotent'), required=True)
    parser.add_argument('--image-tag', required=True)
    parser.add_argument('--bench-image', required=True)
    parser.add_argument('--services-image', required=True)
    parser.add_argument('--materials-context', type=Path, required=True)
    lab = FaultLab()
    lock_path = lab.config.host_data_root / 'runs/.detailed-batch.lock'
    if lock_path.is_symlink():
        raise RuntimeError('I23 execution lock cannot follow links')
    with lock_path.open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lab.run_chain(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
