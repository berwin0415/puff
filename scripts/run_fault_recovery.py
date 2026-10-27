"""I23 Docker 控制传输；源码已编写，仍需 Docker 校准。

DA 使用独立的原状态头崩溃入口。DSH 使用公开会话修复和原持久化 Goal 恢复路径。不合成 I22 暂停索引。
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
        """仅跟踪本采样线程；不序列化命令或响应。"""
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
        # 异常文本、命令、stdout 和 stderr 都可能包含密钥。
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
        """线程已终止不代表采样成功。"""
        try:
            self.sample(event, samples, evidence)
        except BaseException as error:
            self.record_sampling_failure('sampler_thread', error)

    def finish_sampling(self, sampler, outcome):
        sampler.join(timeout=10)
        outcome['sampling_failures'] = list(getattr(self, 'sampling_failures', []))
        if sampler.is_alive():
            raise RuntimeError('I23 采样器未闭合其证据')
        if outcome['sampling_failures']:
            raise RuntimeError('I23 采样器报告证据不完整')

    def failure_exit_transition(self, before, after):
        """仅接受一个确切且绑定来源的退出 1，不泛指任意失败进程。"""
        if (not isinstance(after, dict) or before.get('state', {}).get('Running') is not True
                or any(before.get(key) != after.get(key) for key in ('id', 'name', 'image_id'))):
            raise RuntimeError('stats 缺失不属于确切的受控 Worker 失败')
        attempts = [attempt for attempt, name in self.pins['worker_names'].items()
            if before.get('name') == '/' + name]
        if len(attempts) != 1:
            raise RuntimeError('受控失败未登记为 Worker')
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
            # 在实际创建的对象可能运行或退出前，先登记其 ID。
            # 采样器不能仅凭退出 3 推定它属于预期诊断。
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
                raise RuntimeError('I23 诊断控制进程创建身份不匹配')
            registration = {'name': name, 'id': created, 'action': action,
                'image_id': self.pins['bench_image_id'], 'scope': self.scope,
                'chain_id': self.args.chain_id, 'registered_at': now(), 'before_start': before,
                'crash_boundary_sha256': boundary_hash}
            save(self.paths.run / 'trusted' / f'bench-registration-{self.bench_sequence:04d}.json', registration)
            with self.sampling_lock:
                self.control_registry[name] = registration
            available = timeout - (time.monotonic() - tick)
            if available <= 0:
                raise TimeoutError('I23 诊断控制进程创建已耗尽时间窗口')
            result = self.call(['docker', 'start', '--attach', created], capture=True, check=False, timeout=available)
        else:
            result = self.call(command, capture=True, check=False, timeout=timeout)
        destination = self.paths.run if self.paths.run.exists() else self.paths.fixture
        # 每次轮询使用新名称；不替换已有控制证据。
        with (destination / f'bench-{self.bench_sequence:04d}-{action}.log').open('x') as stream:
            stream.write(result.stdout + result.stderr)
        trusted = self.paths.run / 'trusted'
        if trusted.is_dir():
            save(trusted / f'bench-process-{self.bench_sequence:04d}.json', {
                'name': name, 'action': action, 'after_exit': self.snapshot(name), 'exit_code': result.returncode})
        if check and result.returncode:
            raise RuntimeError('I23 独立 bench 拒绝了 ' + action)
        return result

    def diagnostic_exit_transition(self, before, after):
        """分类一次缺失 stats 的竞态；最终 bench 独立复核。"""
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
            raise RuntimeError('stats 缺失不属于已登记的 I23 诊断退出')
        trusted = self.paths.run / 'trusted'
        for filename in ('worker-b-binding.json', 'worker-b-created.json', 'worker-b.json'):
            path = trusted / filename
            if path.exists() or path.is_symlink():
                raise RuntimeError('I23 诊断不能发生在 Worker B 绑定或创建之后')
        def material(path):
            if (any(parent.is_symlink() for parent in (path, *path.parents))
                    or not path.is_file() or path.stat().st_size > 8 * 1024**2):
                raise RuntimeError('I23 诊断材料必须是有大小限制的普通文件')
            data = path.read_bytes()
            if len(data) > 8 * 1024**2:
                raise RuntimeError('I23 诊断材料超过大小限制')
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
            raise RuntimeError('I23 诊断缺口未绑定原始崩溃边界')
        return {'event': 'diagnostic_exit_during_sample', 'id': before['id'], 'before': before, 'after': after,
            'control_action': 'bind-crash-resume', 'registered_name': name,
            'gap_sha256': hashlib.sha256(gap_data).hexdigest(), 'crash_boundary_sha256': boundary_hash}

    def sample(self, event, samples, evidence):
        """仅覆盖 I23 行为；I22 保留严格的健康退出采样规则。"""
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
                            raise RuntimeError('Docker stats 包含重复或未保留的容器')
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
                            raise RuntimeError('采样时发现已认定退出的 Worker 又在运行')
                        sample['worker_ids'] = sorted(set(role_ids) | {r['id'] for r in active if r['image_id'] in self.candidate_image_ids
                            or r['name'].removeprefix('/') in self.pins['worker_names'].values()})
                    except Exception as error:
                        diagnostic['sampling_error_type'] = self.sampling_error_type(error)
                        sample['error'] = 'Docker 资源采样失败'
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
                # 本次追加发生在原采样锁外。下一条记录将循环入口
                # 与本条完成写入关联；
                # 不改写原样本或其时间戳。
                try:
                    with (evidence / 'resource-sampling-timing.jsonl').open('a') as stream:
                        stream.write(json.dumps(diagnostic) + '\n')
                except BaseException as error:
                    # 新诊断写入不能遮蔽原始采样异常。
                    # 等待线程结束后的主链会检查此固定标志。
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
            raise RuntimeError('绑定任何 Worker 前，工具面必须已冻结在不变的链固定材料中')
        save(path, self.pins)

    def calibrate_surface(self):
        super().calibrate_surface()
        self.freeze_calibrated_surface_pins()

    def identity(self, attempt):
        return {'schema': 'harness-lab-fault-worker/1', 'candidate': self.args.candidate,
            'chain_id': self.args.chain_id, 'scope': self.scope, 'attempt_id': attempt}

    def remaining(self):
        # I23 使用一个连续活跃窗口，包含控制和重启耗时。
        # 窗口在创建 A 前开始，最后一个 Worker 退出后才结束。
        return 300 - (time.monotonic() - self.active_tick)

    def explicit_mounts(self, attempt, binding):
        config = self.paths.fixture / 'attempts' / attempt
        mounts = [(config / 'request.json', '/config/request.json', True),
            (self.paths.fixture / 'scope.json', '/config/scope.json', True),
            (self.paths.fixture / 'scope.token', '/config/scope.token', True),
            (self.paths.budget / 'run.token', '/budget/run.token', True),
            (self.paths.state, '/state', False), (self.paths.workspace, '/workspace', False),
            (self.paths.run / 'attempts' / attempt / 'candidate', '/evidence', False)]
        # 经审查的原生绑定器仅可添加属于本次 attempt 的确切单文件控制挂载，
        # 不能挂载整个配置或状态父目录以及可信目录树。
        for filename in binding.get('native_control_files', []):
            allowed = {'native-crash.json'} if self.args.candidate == 'deepagents_langgraph' else {'fault-goal.json', 'fault-state.json'}
            if filename not in allowed:
                raise RuntimeError('原生控制文件不符合预期')
            mounts.append((config / filename, '/config/' + filename, True))
        if self.args.fault == 'kill_before_write':
            mounts += [(config / 'prewrite.json', '/config/prewrite.json', True),
                       (config / 'prewrite.token', '/config/prewrite.token', True)]
        if len({destination for _, destination, _ in mounts}) != len(mounts):
            raise RuntimeError('显式 Worker 目标路径重复')
        for source, _, _ in mounts:
            if not source.exists() or source.is_symlink() or any(parent.is_symlink() for parent in source.parents):
                raise RuntimeError('Worker 挂载缺失或使用了链接')
        return mounts

    def worker(self, attempt):
        binding_file = self.paths.run / 'trusted' / (attempt + '-binding.json')
        binding = json.loads(binding_file.read_text())
        if (binding.get('schema') != 'harness-lab-fault-worker-binding/1' or binding.get('scope') != self.scope
                or binding.get('chain_id') != self.args.chain_id or binding.get('attempt_id') != attempt
                or binding.get('candidate_image_id') != self.pins['candidate_image_id']
                or binding.get('model_token_sha256') != hashlib.sha256((self.paths.budget / 'run.token').read_bytes().strip()).hexdigest()
                or binding.get('mode') != ('native-start' if attempt == 'worker-a' else 'native-crash-resume')):
            raise RuntimeError('Worker 原生绑定身份不匹配')
        request_file = self.paths.fixture / 'attempts' / attempt / 'request.json'
        if hashlib.sha256(request_file.read_bytes()).hexdigest() != binding.get('request_sha256'):
            raise RuntimeError('Worker 请求在独立原生绑定后发生变化')
        request = json.loads(request_file.read_text())
        original = json.loads((self.paths.fixture / 'candidate-request.json').read_text())
        if attempt == 'worker-a' and request.get('prompt') != original['prompt']:
            raise RuntimeError('原始目标在首次提交前发生变化')
        if attempt == 'worker-b' and any(key in request for key in ('prompt', 'followup')):
            raise RuntimeError('崩溃恢复不能重提提示词或后续输入')
        argv = binding.get('argv')
        if (not isinstance(argv, list) or not argv or any(not isinstance(word, str) for word in argv)
                or argv[-2:] != ['--request', '/config/request.json']
                or binding.get('entrypoint') not in {'/opt/venv/bin/python', 'node'}):
            raise RuntimeError('需要经审查且绑定镜像原生入口的材料')
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
        self.names.append(name)  # 即使 create 超时，也可能留下此对象。
        record = {**self.identity(attempt), 'started_at': now(), 'kill': None, 'deleted': None,
                  'errors': [], 'binding_sha256': hashlib.sha256(binding_file.read_bytes()).hexdigest()}
        identifier_ = self.call(command, capture=True, timeout=max(0.1, min(30, self.remaining()))).stdout.strip()
        before = self.snapshot(identifier_)
        if (not before or before['id'] != identifier_ or before['name'] != '/' + name
                or before['image_id'] != self.pins['candidate_image_id'] or before['state'].get('Status') != 'created'
                or before['state'].get('Running') is not False or before['memory_limit'] != 2 * 1024**3 or before['cpu_nano'] != 10**9):
            raise RuntimeError('创建的 Worker 身份不符合预期')
        by_dest = {row['Destination']: row for row in before['mounts']}
        if (len(by_dest) != len(before['mounts']) or set(by_dest) != {dest for _, dest, _ in mounts}
                or any(by_dest[dest]['Source'] != str(source) or by_dest[dest]['RW'] is readonly
                       for source, dest, readonly in mounts)):
            raise RuntimeError('创建的 Worker 挂载不符合预期')
        record['before_start'] = before
        save(self.paths.run / 'trusted' / (attempt + '-created.json'), record)
        self.sampling_phase = attempt + '_active'
        if self.remaining() <= 0:
            raise TimeoutError('Worker 启动前 I23 累计活跃时间窗口已用尽')
        process = None
        try:
            with (self.paths.run / 'attempts' / attempt / 'candidate.log').open('x') as stream:
                process = subprocess.Popen(['docker', 'start', '--attach', identifier_], cwd=ROOT,
                    env=self.env, text=True, stdout=stream, stderr=subprocess.STDOUT)
                while process.poll() is None:
                    if self.remaining() <= 0:
                        self.timed_out = True
                        self.call(['docker', 'kill', identifier_], capture=True, check=False, timeout=10)
                        raise TimeoutError('I23 累计活跃时间窗口已用尽')
                    if attempt == 'worker-a' and self.args.fault == 'kill_before_write':
                        observed = self.bench('observe-gate', check=False, timeout=max(0.1, min(10, self.remaining())))
                        if observed.returncode != 0:
                            raise RuntimeError('可信写前观察失败')
                        if (self.paths.run / 'trusted/gate-signal.json').is_file():
                            signal_file = self.paths.run / 'trusted/gate-signal.json'
                            # 并发 stats 采集避开 running → injected137 边界。
                            # attach 等待留在锁外；
                            # 删除使用独立锁，保护容器清单一致性。
                            with self.sampling_lock:
                                lock_tick = time.monotonic()
                                live = self.snapshot(identifier_)
                                if (not live or live['id'] != identifier_ or live['image_id'] != self.pins['candidate_image_id']
                                        or live['state'].get('Running') is not True or live['state'].get('OOMKilled') is not False):
                                    raise RuntimeError('门禁触发时未独立证明确切 Worker 存活')
                                record['before_kill'] = live
                                record['kill'] = {'signal': 'SIGKILL', 'container_id': identifier_, 'sent_at': now(),
                                    'gate_signal_sha256': hashlib.sha256(signal_file.read_bytes()).hexdigest()}
                                result = self.call(['docker', 'kill', '--signal', 'SIGKILL', identifier_], capture=True, check=False, timeout=10)
                                record['kill']['exit_code'] = result.returncode
                                if result.returncode:
                                    raise RuntimeError('Docker 未确认确切 ID 的 SIGKILL')
                                record['after_exit'] = self.snapshot(identifier_)
                                stopped = record['after_exit']
                                state = stopped.get('state', {}) if isinstance(stopped, dict) else {}
                                if (not stopped or stopped.get('id') != identifier_ or stopped.get('name') != '/' + name
                                        or stopped.get('image_id') != self.pins['candidate_image_id']
                                        or state.get('Status') != 'exited' or type(state.get('Pid')) is not int or state['Pid'] != 0
                                        or type(state.get('ExitCode')) is not int or state['ExitCode'] != 137
                                        or any(state.get(key) is not False for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
                                        or state.get('Error') != ''):
                                    raise RuntimeError('SIGKILL 后未独立确认确切 Worker 退出 137')
                                record['finished_at'] = now()
                                record['kill_observation_lock_seconds'] = time.monotonic() - lock_tick
                            process.wait(timeout=max(0.1, min(10, self.remaining())))
                            # containers() 先列出 ID，再逐个检查。
                            # 不在这两次观察之间删除 A。
                            with self.sampling_lock:
                                self.call(['docker', 'rm', identifier_], capture=True, timeout=10)
                                record['deleted'] = {'id_absent': self.snapshot(identifier_) is None,
                                    'name_absent': self.snapshot(name) is None, 'observed_at': now()}
                                if not record['deleted']['id_absent'] or not record['deleted']['name_absent']:
                                    raise RuntimeError('未独立观察到确切被杀 Worker 已删除')
                            break
                    time.sleep(0.25)
                process.wait(timeout=max(0.1, min(10, self.remaining())))
                record['attach_exit'] = process.returncode
        finally:
            # 在后续证据 I/O 可能失败并遗留付费执行前，先停止服务工作。
            # Docker rm 仍用于通用清理。
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
            raise RuntimeError('请先清理现有实验容器')
        self.config.initialize_layout()
        self.paths = self.config.paths_for('fault-recovery', args.chain_id, args.candidate, 'alice', 'main', args.chain_id, case='fault-recovery')
        if any(path.exists() or path.is_symlink() for path in vars(self.paths).values()):
            raise RuntimeError('故障链 ID 已使用')
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
            raise RuntimeError('缺少所选远程模型名称')
        self.env['LAB_MODEL_NAME'] = model
        context = args.materials_context.resolve()
        if 'old' in context.parts or not context.is_relative_to(ROOT / 'external/build-context'):
            raise RuntimeError('I23 材料上下文必须是当前独立目录')
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
        # 真实上游凭证仅由 model-proxy 继承。
        # 展开的 compose 和 Config.Env 都不写入证据。
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
            # 运行代理不会产生模型请求。只有独立审查的原生启动绑定
            # 存在后，才创建候选。
            binding = self.bench('bind-start', check=False)
            if binding.returncode == 3:
                outcome['native_start_available'] = False
                return 3
            if binding.returncode:
                raise RuntimeError('原生启动绑定器拒绝了 I23 变体')
            outcome['native_start_available'] = True
            sampler = threading.Thread(target=self.sample_with_reporting, args=(stop, samples, self.paths.run), daemon=True)
            sampler.start()
            outcome['active_started_at'] = now()
            self.active_tick = time.monotonic()
            a = self.worker('worker-a')
            if args.fault == 'kill_before_write' and a['kill'] is not None:
                # 采集控制与状态、重新创建 Worker B 的耗时
                # 均计入同一个 300 秒活跃窗口。
                self.bench('after-kill', timeout=max(0.1, min(20, self.remaining())))
                save(self.paths.run / 'trusted/resume-clock.json', {'chain_id': args.chain_id, 'scope': self.scope,
                    'used_active_seconds': time.monotonic() - self.active_tick,
                    'boundary_sha256': hashlib.sha256((self.paths.run / 'trusted/crash-boundary.json').read_bytes()).hexdigest()})
                bound = self.bench('bind-crash-resume', check=False, timeout=max(0.1, min(20, self.remaining())))
                if bound.returncode == 0:
                    self.worker('worker-b')
                elif bound.returncode != 3:
                    raise RuntimeError('原生崩溃恢复绑定器拒绝了保留状态')
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
        raise RuntimeError('I23 执行锁不能跟随链接')
    with lock_path.open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lab.run_chain(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
