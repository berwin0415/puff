"""I22 暂停及替换实验的宿主 Docker 控制。

宿主进程不导入候选、不调用模型、不准备业务，也不评分。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from scripts.lab import CANDIDATES, Lab
from runner.bench import identifier
from runner.metrics import summarize


def now():
    return datetime.now(timezone.utc).isoformat()


def save(path, value):
    temporary = path.with_suffix('.json.tmp')
    with temporary.open('w') as stream:
        temporary.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    temporary.replace(path)


def rows(path):
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return []
    result = []
    for number, line in enumerate(data.splitlines(keepends=True), 1):
        if not line.endswith(b'\n'):
            continue
        row = json.loads(line)
        result.append((number, line, row))
    return result


class PauseLab(Lab):
    def snapshot(self, identity):
        reply = self.call(['docker', 'inspect', identity], capture=True, check=False, timeout=10)
        if reply.returncode:
            if re.fullmatch(r'(?:error(?: response from daemon)?:\s*)?no such (?:object|container):\s*'
                    + re.escape(identity), reply.stderr.strip(), flags=re.IGNORECASE) and reply.stdout.strip() == '[]':
                return None
            raise RuntimeError('Docker 身份检查失败；尚未证明对象不存在')
        data = json.loads(reply.stdout)
        if len(data) != 1:
            raise RuntimeError('Docker 身份不唯一')
        c = data[0]
        return {'id': c['Id'], 'name': c['Name'], 'image_id': c['Image'],
            'image_tag': c['Config']['Image'], 'state': c['State'],
            'memory_limit': c['HostConfig']['Memory'], 'cpu_nano': c['HostConfig']['NanoCpus'],
            'mounts': [{k: m[k] for k in ('Source', 'Destination', 'RW')} for m in c['Mounts']]}

    def bench_command(self):
        command = ['docker', 'run', '--rm', '--label', 'com.docker.compose.project=harness-lab',
            '--label', 'org.harness-lab.role=acceptance', '--network', 'harness-lab_lab',
            '--read-only', '--memory', '512m', '--cpus', '0.5', '--pids-limit', '128',
            '--tmpfs', '/tmp:size=128m', '-e', 'HARNESS_LAB_ENVIRONMENT=container']
        for name in ('state', 'workspaces', 'fixtures', 'budgets', 'runs'):
            command += ['--mount', f'type=bind,src={self.config.host_data_root / name},dst=/data/{name}']
        return command + ['--entrypoint', 'python', self.pins['bench_image_id']]

    def wait_health(self, service, port, *, container=None):
        health_host = '127.0.0.1' if container else service
        code = ('import httpx,time\n'
            'for i in range(30):\n'
            ' try:\n'
            f'  r=httpx.get("http://{health_host}:{port}/health",timeout=1,trust_env=False); r.raise_for_status(); break\n'
            ' except httpx.HTTPError: time.sleep(0.3)\n'
            'else: raise SystemExit("服务就绪检查超时")\n')
        self.call(['docker', 'exec', container or 'harness-lab-' + service + '-1', 'python', '-c', code], capture=True, timeout=30)

    def replace_idle_proxy(self, expected_old_id):
        """采样期间预热替换代理；仅删除操作使用短锁。

        本阶段没有 Worker。两个代理共享原预算数据库和上限；其临时资源分配仍由采样器记录。
        """
        original = 'harness-lab-model-proxy-1'
        before = self.snapshot(original)
        if (before is None or before['id'] != expected_old_id or before['image_id'] != self.service_images['model-proxy']
                or before['memory_limit'] != 256 * 1024**2 or before['cpu_nano'] != 250000000
                or before['state'].get('Running') is not True
                or before['mounts'] != [{'Source': str(self.paths.budget), 'Destination': '/budget', 'RW': True}]):
            raise RuntimeError('原代理与固定镜像、预算挂载或配额不同')
        worker_ids = self.call(['docker', 'ps', '-q', '--no-trunc',
            '--filter', 'label=com.docker.compose.project=harness-lab',
            '--filter', 'label=org.harness-lab.role=worker'], capture=True, timeout=5).stdout.split()
        if worker_ids:
            raise RuntimeError('代理替换需要没有 Worker 的时间窗口')
        name = 'harness-lab-proxy-replacement-' + self.args.chain_id
        self.names.append(name)
        # 私下传递已有代理环境，不显示其内容，
        # 也不使用其他解析器展开用户 .env。
        env_reply = self.call(['docker', 'inspect', '--format', '{{json .Config.Env}}', before['id']], capture=True)
        values = json.loads(env_reply.stdout)
        if not isinstance(values, list) or any(not isinstance(v, str) or '\n' in v or '\r' in v for v in values):
            raise RuntimeError('无法原样传递代理环境')
        environment_sha = hashlib.sha256(json.dumps(sorted(values), separators=(',', ':')).encode()).hexdigest()
        expected_command = ['-m', 'execution.model_proxy', '--database', '/budget/calls.sqlite',
            '--run-token-file', '/budget/run.token', '--cap', '20', '--port', '8082']
        actual_command = json.loads(self.call(['docker', 'inspect', '--format', '{{json .Config.Cmd}}',
            before['id']], capture=True).stdout)
        if actual_command != expected_command:
            raise RuntimeError('原代理命令与冻结预算配置不同')
        private_env = self.paths.fixture / 'proxy-replacement.env'
        try:
            descriptor = os.open(private_env, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, 'w') as stream:
                stream.write('\n'.join(values) + '\n')
            del values, env_reply
            command = ['docker', 'create', '--name', name,
                '--label', 'com.docker.compose.project=harness-lab',
                '--label', 'com.docker.compose.service=model-proxy',
                '--label', 'org.harness-lab.role=model-proxy',
                '--network', 'harness-lab_lab', '--network-alias', 'model-proxy',
                '--read-only', '--memory', '256m', '--cpus', '0.25', '--pids-limit', '64',
                '--tmpfs', '/tmp:size=32m', '--env-file', str(private_env),
                '--mount', f'type=bind,src={self.paths.budget},dst=/budget',
                self.service_images['model-proxy'], *expected_command]
            identity = self.call(command, capture=True, timeout=30).stdout.strip()
            if not re.fullmatch('[0-9a-f]{64}', identity):
                raise RuntimeError('代理创建未返回确切容器 ID')
            self.names.append(identity)
        finally:
            private_env.unlink(missing_ok=True)
        created = self.snapshot(identity)
        if (created is None or created['image_id'] != before['image_id'] or created['mounts'] != before['mounts']
                or created['memory_limit'] != before['memory_limit'] or created['cpu_nano'] != before['cpu_nano']):
            raise RuntimeError('替换代理与原执行固定值不同')
        replacement_env = json.loads(self.call(['docker', 'inspect', '--format', '{{json .Config.Env}}', identity], capture=True).stdout)
        replacement_sha = hashlib.sha256(json.dumps(sorted(replacement_env), separators=(',', ':')).encode()).hexdigest()
        del replacement_env
        replacement_command = json.loads(self.call(['docker', 'inspect', '--format', '{{json .Config.Cmd}}', identity], capture=True).stdout)
        if replacement_sha != environment_sha or replacement_command != expected_command:
            raise RuntimeError('替换代理环境或命令已变化')
        self.call(['docker', 'start', identity], capture=True, timeout=20)
        self.wait_health('model-proxy', 8082, container=identity)
        with self.sampling_lock:
            started = time.monotonic()
            # 此时没有待中断调用。启动和健康检查留在锁外，
            # 避免服务重建掩盖长时间采样缺口。
            self.call(['docker', 'stop', '--time', '1', before['id']], capture=True, timeout=10)
            stopped = self.snapshot(before['id'])
            if (stopped is None or stopped['id'] != before['id']
                    or stopped['state'].get('Status') != 'exited' or stopped['state'].get('Pid') != 0
                    or stopped['state'].get('Running') is not False or stopped['state'].get('OOMKilled') is not False
                    or stopped['state'].get('Error') or stopped['state'].get('ExitCode') not in (0, 137, 143)):
                raise RuntimeError('旧代理未达到预期的外部停止状态')
            self.call(['docker', 'rm', before['id']], capture=True, timeout=10)
            removed = self.snapshot(before['id']) is None
            if not removed:
                raise RuntimeError('未独立观察到旧代理已删除')
            self.call(['docker', 'rename', identity, original], capture=True, timeout=10)
            self.names.append(original)
            lock_seconds = time.monotonic() - started
        after = self.snapshot(identity)
        return {'before_id': before['id'], 'after_id': identity, 'at': now(),
            'before': before, 'old_after_stop': stopped, 'replacement': after,
            'replacement_created': created, 'old_id_absent': removed,
            'environment_sha256': environment_sha, 'replacement_environment_sha256': replacement_sha,
            'command': expected_command, 'replacement_command': replacement_command,
            'sampling_lock_seconds': lock_seconds,
            'transition': 'replacement_ready_before_old_proxy_removed; same_budget_no_worker'}

    def bench(self, action, *, host_record=None, extra=(), check=True):
        name = 'harness-lab-pause-bench-' + self.args.chain_id + '-' + action
        self.names.append(name)
        # 即使 bench 很快退出，也保留确切 ID，供采样检查。
        command = self.bench_command()
        command.remove('--rm')
        command[2:2] = ['--name', name]
        command += ['-m', 'runner.pause_recovery', action,
            '--candidate', self.args.candidate, '--chain-id', self.args.chain_id,
            '--seed', str(self.args.seed), '--date', self.day]
        if host_record is not None:
            command += ['--host-record', str(host_record).replace(str(self.config.host_data_root), '/data', 1)]
        result = self.call(command + list(extra), capture=True, check=False, timeout=90)
        log_root = self.paths.run if self.paths.run.is_dir() else self.paths.fixture
        with (log_root / ('bench-' + action + '.log')).open('x') as log:
            log.write(result.stdout + result.stderr)
        if check and result.returncode:
            raise RuntimeError('独立 bench 拒绝了 ' + action + '；保留日志包含诊断信息')
        return result

    def sample(self, event, samples, evidence):
        while not event.is_set():
            tick = time.monotonic()
            with self.sampling_lock:
                phase = getattr(self, 'sampling_phase', 'services_idle')
                sample = {'unix_time': time.time(), 'at': now(), 'phase': phase, 'containers': [], 'lifecycle': [], 'error': None}
                try:
                    records = [r for r in self.containers() if r['state']['Running']]
                    ids = [r['id'] for r in records]
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
                        # 正常退出可能发生在首次活跃容器清单之后、
                        # stats 之前。检查确切 ID；缺失、不健康或仍在运行
                        # 都不能当作零消耗。
                        after = self.snapshot(record['id'])
                        state = after['state'] if after else {}
                        if (not after or after['id'] != record['id'] or after['image_id'] != record['image_id']
                                or after['name'] != record['name'] or state.get('Status') != 'exited'
                                or state.get('Pid') != 0 or state.get('ExitCode') != 0
                                or any(state.get(key) is not False for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
                                or state.get('Error') != ''):
                            raise RuntimeError('stats 缺失，且未独立证明确切 ID 已健康退出')
                        sample['lifecycle'].append({'event': 'exited_during_sample', 'id': record['id'],
                            'before': record, 'after': after})
                        exited_ids.add(record['id'])
                    records = active
                    for record in records:
                        name = record['name'].removeprefix('/')
                        role = 'worker' if record['image_id'] in self.candidate_image_ids or name in self.pins['worker_names'].values() else 'bench' if record['image_id'] == self.pins['bench_image_id'] else 'shared_service'
                        sample['containers'].append({**by_name.get(name, {}), 'id': record['id'], 'role': role,
                            'memory_limit': record['memory_limit'], 'cpu_nano': record['cpu_nano']})
                    role_ids = self.call(['docker', 'ps', '-q', '--no-trunc', '--filter', 'label=com.docker.compose.project=harness-lab',
                        '--filter', 'label=org.harness-lab.role=worker'], capture=True, timeout=5).stdout.split()
                    if exited_ids & set(role_ids):
                        raise RuntimeError('采样时发现已认定退出的 Worker 又在运行')
                    sample['worker_ids'] = sorted(set(role_ids) | {r['id'] for r in records if r['image_id'] in self.candidate_image_ids
                        or r['name'].removeprefix('/') in self.pins['worker_names'].values()})
                except Exception:
                    sample['error'] = 'Docker 资源采样失败'
                    sample['worker_ids'] = None
                samples.append(sample)
                with (evidence / 'resource-samples.jsonl').open('a') as stream:
                    stream.write(json.dumps(sample) + '\n')
            event.wait(max(0, 2 - (time.monotonic() - tick)))

    def wait_after_delete(self, a):
        self.sampling_phase = 'paused_wait'
        started, tick = now(), time.monotonic()
        monitor_name = 'harness-lab-wait-monitor-' + self.args.chain_id
        self.names.append(monitor_name)
        command = ['docker', 'run', '--name', monitor_name,
            '--label', 'com.docker.compose.project=harness-lab', '--label', 'org.harness-lab.role=acceptance',
            '--network', 'harness-lab_lab', '--read-only', '--memory', '512m', '--cpus', '0.5',
            '--pids-limit', '128', '--tmpfs', '/tmp:size=128m', '-e', 'HARNESS_LAB_ENVIRONMENT=container']
        for source, writable in ((self.paths.run, True), (self.paths.budget, False)):
            target = str(source).replace(str(self.config.host_data_root), '/data', 1)
            command += ['--mount', f'type=bind,src={source},dst={target}' + ('' if writable else ',readonly')]
        command += ['--entrypoint', 'python', self.pins['bench_image_id'],
            '-m', 'runner.pause_recovery', 'wait-monitor', '--candidate', self.args.candidate,
            '--chain-id', self.args.chain_id, '--seed', str(self.args.seed), '--date', self.day]
        proxy_before = next(c['id'] for c in self.containers() if c['name'].removeprefix('/') == 'harness-lab-model-proxy-1')
        restarts = []
        with (self.paths.run / 'bench-wait-monitor.log').open('x') as log:
            monitor = subprocess.Popen(command, cwd=ROOT, env=self.env, text=True, stdout=log, stderr=subprocess.STDOUT)
            try:
                while monitor.poll() is None:
                    elapsed = time.monotonic() - tick
                    if self.args.restart_proxy and elapsed >= 300 and not restarts:
                        replacement = self.replace_idle_proxy(proxy_before)
                        if replacement['before_id'] != proxy_before:
                            raise RuntimeError('替换前原代理身份已变化')
                        restarts.append(replacement)
                    if elapsed > 660:
                        raise TimeoutError('受限等待监控器未结束')
                    time.sleep(0.5)
                if monitor.returncode:
                    raise RuntimeError('独立等待监控器拒绝了暂停区间')
                # 从首份保留的等待样本起，覆盖完整 600 秒。
                while time.monotonic() - tick < 604:
                    time.sleep(0.5)
            finally:
                if monitor.poll() is None:
                    self.call(['docker', 'rm', '-f', monitor_name], capture=True, check=False, timeout=10)
                    monitor.terminate()
                    monitor.wait(timeout=10)
        # 完成一次采集后再闭合采样阶段，不丢失待处理等待样本。
        with self.sampling_lock:
            self.sampling_phase = 'after_wait'
        record = {'schema': 'harness-lab-pause-wait-host/1', 'candidate': self.args.candidate,
            'chain_id': self.args.chain_id, 'started_at': started, 'finished_at': now(),
            'worker_a_deleted': {'id': a['after_exit']['id'], 'id_absent': self.snapshot(a['after_exit']['id']) is None,
                'name_absent': self.snapshot(self.pins['worker_names']['worker-a']) is None},
            'resources_file': 'trusted/wait-resource-samples.jsonl', 'budget_samples_file': 'wait-budget-samples.jsonl',
            'budget_summary_file': 'wait-budget-summary.json', 'proxy_restarts': restarts, 'errors': []}
        interval = [s for s in self.samples if s['phase'] == 'paused_wait']
        first = interval[0]['unix_time'] if interval else None
        with (self.paths.run / record['resources_file']).open('x') as stream:
            for sample in interval:
                stream.write(json.dumps({'at': sample['at'], 'elapsed_seconds': sample['unix_time'] - first,
                    'worker_ids': sample['worker_ids'], 'containers': sample['containers'],
                    'docker_stats_raw': sample.get('docker_stats_raw'), 'lifecycle': sample.get('lifecycle', []),
                    'error': sample['error']}) + '\n')
        save(self.paths.run / 'trusted/wait.json', record)
        return record

    def budget_count(self):
        code = ('import sqlite3; c=sqlite3.connect("file:/budget/calls.sqlite?mode=ro",uri=True); '
                'print(c.execute("SELECT COUNT(*) FROM calls").fetchone()[0])')
        return int(self.call(['docker', 'exec', 'harness-lab-model-proxy-1',
            'python', '-c', code], capture=True, timeout=10).stdout.strip())

    def calibrate_surface(self):
        name = 'harness-lab-surface-' + self.args.chain_id
        self.names.append(name)
        calibration = self.paths.fixture / 'surface'
        calibration.mkdir()
        output = self.paths.run / 'surface-calibration'
        output.mkdir()
        workspace = calibration / 'workspace'
        workspace.mkdir()
        request = {'session_id': 'main', 'workspace': '/workspace', 'output_dir': '/evidence',
            'tools_url': 'http://tools:8081', 'tool_token_file': '/config/scope.token',
            'trusted_scope_file': '/config/scope.json', 'max_output_tokens': 4096, 'max_retries': 2}
        save(calibration / 'request.json', request)
        command = ['docker', 'create', '--name', name, '--platform', 'linux/arm64',
            '--label', 'com.docker.compose.project=harness-lab', '--label', 'org.harness-lab.role=calibration',
            '--network', 'harness-lab_lab', '--read-only', '--memory', '2g', '--cpus', '1',
            '--pids-limit', '256', '--tmpfs', '/tmp:size=128m',
            '-e', 'MODEL_NAME=' + self.pins['model_name'],
            '-e', 'HARNESS_LIFECYCLE_IMAGE_ID=' + self.pins['candidate_image_id'], '--entrypoint', 'node']
        for source, destination, readonly in ((calibration / 'request.json', '/config/surface.json', True),
                (self.paths.fixture / 'scope.json', '/config/scope.json', True),
                (self.paths.fixture / 'scope.token', '/config/scope.token', True),
                (workspace, '/workspace', False), (output, '/evidence', False)):
            command += ['--mount', f'type=bind,src={source},dst={destination}' + (',readonly' if readonly else '')]
        command += [self.pins['candidate_image_id'], '--import', 'tsx/esm',
            '/opt/materials/harness-lab/goal-surface-entrypoint.ts', '--request', '/config/surface.json']
        identity = self.call(command, capture=True, timeout=30).stdout.strip()
        before = self.snapshot(identity)
        record = {'before_start': before, 'model_call_count_before': self.budget_count(),
            'real_model_token_mounted': False}
        with (output / 'candidate.log').open('x') as log:
            result = subprocess.run(['docker', 'start', '--attach', identity], cwd=ROOT, env=self.env,
                text=True, stdout=log, stderr=subprocess.STDOUT, timeout=90)
        after = self.snapshot(identity)
        record.update({'after_exit': after, 'attach_exit': result.returncode,
            'process_exit': after['state']['ExitCode'], 'oom_killed': after['state']['OOMKilled'],
            'model_call_count_after': self.budget_count()})
        save(output / 'host.json', record)
        if (before['image_id'] != self.pins['candidate_image_id'] or result.returncode != 0
                or record['process_exit'] != 0 or record['oom_killed']
                or record['model_call_count_before'] != 0 or record['model_call_count_after'] != 0):
            raise RuntimeError('无模型原生工具面校准失败')
        surface_bytes = (output / 'goal-tool-surface.json').read_bytes()
        self.pins['tool_surface_sha256'] = hashlib.sha256(surface_bytes).hexdigest()
        with (self.paths.run / 'trusted/tool-surface.json').open('xb') as stream:
            stream.write(surface_bytes)
        save(self.paths.run / 'trusted/tool-surface-host.json', {
            'schema': 'harness-lab-goal-surface-host/1', 'candidate_image_id': before['image_id'],
            'source_export_sha256': self.pins['source_export_sha256'],
            'adapter_export_sha256': self.pins['adapter_export_sha256'],
            'surface_sha256': self.pins['tool_surface_sha256'], 'process_exit': record['process_exit'],
            'oom_killed': record['oom_killed'], 'model_call_count_before': record['model_call_count_before'],
            'model_call_count_after': record['model_call_count_after'], 'real_model_token_mounted': False})
        self.call(['docker', 'rm', identity], capture=True, timeout=10)

    def identity(self, attempt):
        return {'schema': 'harness-lab-pause-host/1', 'candidate': self.args.candidate,
            'chain_id': self.args.chain_id, 'scope': self.scope, 'attempt_id': attempt}

    def pause_trigger(self, container_id, attempt, stop):
        directory = self.paths.run / 'attempts' / attempt / 'candidate'
        source_path = directory / 'raw-events.jsonl'
        while not stop.wait(0.1):
            try:
                observation_started = time.monotonic()
                controls = rows(directory / 'control-events.jsonl')
                ready = any(row.get('phase') == 'pause_handler_installed'
                    and row.get('signal') == 'SIGUSR2' and row.get('scope') == self.scope
                    and row.get('chain_id') == self.args.chain_id and row.get('attempt_id') == attempt
                    for _, _, row in controls)
                if not ready:
                    continue
                tool_records = rows(self.paths.run / 'tool-events.jsonl')
                writes = [(number, line, row) for number, line, row in tool_records
                    if row.get('scope') == self.scope and row.get('tool') == 'write'
                    and row.get('phase') == 'finish' and row.get('status') == 200
                    and isinstance(row.get('result', {}).get('receipt'), dict)]
                if not writes:
                    continue
                active = {}
                raw_records = rows(source_path)
                for number, line, row in raw_records:
                    if self.args.candidate == 'deepagents_langgraph' and row.get('scope') != self.scope:
                        continue
                    if self.args.candidate == 'deepagents_langgraph':
                        event = row.get('data', {})
                        if row.get('stream_mode') == 'tasks' and isinstance(event, dict) and isinstance(event.get('id'), str):
                            if 'input' in event and 'result' not in event:
                                active[event['id']] = (number, line, event.get('name'))
                            elif any(key in event for key in ('result', 'error', 'interrupts')):
                                active.pop(event['id'], None)
                        if row.get('type') == 'native_graph_drained':
                            active.clear()
                    else:
                        native_header = json.loads((directory / 'session-before-followup.json').read_text())['header']
                        if row.get('params', {}).get('sessionId') != native_header['id']:
                            continue
                        event = row.get('params', {}).get('event', {})
                        if event.get('type') == 'turn/start':
                            active = {'turn': (number, line, 'turn')}
                        elif event.get('type') == 'turn/end':
                            active.clear()
                activity = [entry for entry in active.values() if entry[2] in {'model', 'tools', 'turn'}]
                if not activity:
                    continue
                control_name = 'control-events.jsonl' if self.args.candidate == 'deepagents_langgraph' else 'pause-events.jsonl'
                try:
                    control_bytes = (directory / control_name).read_bytes()
                except FileNotFoundError:
                    control_bytes = b''
                if control_bytes and not control_bytes.endswith(b'\n'):
                    continue
                control_records = [json.loads(line) for line in control_bytes.splitlines()]
                if any(row.get('phase') == 'requested' for row in control_records):
                    raise ValueError('本控制器发信号前已存在暂停请求')
                observation_finished = time.monotonic()
                number, line, _ = max(activity)
                write_number, write_line, _ = writes[0]
                prefix = f'attempts/{attempt}/candidate/'
                request = {'sent_at': now(), 'signal': 'SIGUSR2', 'container_id': container_id,
                    'observation_clock': 'host_monotonic_prefix/1',
                    'observation_started_monotonic': observation_started,
                    'observation_finished_monotonic': observation_finished,
                    'signal_sent_monotonic': time.monotonic(),
                    'activity_source': {'path': prefix + 'raw-events.jsonl', 'line': number,
                        'line_sha256': hashlib.sha256(line).hexdigest(),
                        'observed_through_line': raw_records[-1][0],
                        'observed_prefix_sha256': hashlib.sha256(b''.join(record[1] for record in raw_records)).hexdigest()},
                    'write_source': {'path': 'tool-events.jsonl', 'line': write_number,
                        'line_sha256': hashlib.sha256(write_line).hexdigest(),
                        'observed_through_line': tool_records[-1][0],
                        'observed_prefix_sha256': hashlib.sha256(b''.join(row[1] for row in tool_records)).hexdigest()},
                    'control_source': {'path': prefix + control_name,
                        'observed_through_line': len(control_records),
                        'observed_prefix_sha256': hashlib.sha256(control_bytes).hexdigest()}}
                result = self.call(['docker', 'kill', '--signal', 'SIGUSR2', container_id], capture=True, check=False, timeout=10)
                request['signal_exit_code'] = result.returncode
                request['signal_acknowledged_monotonic'] = time.monotonic()
                self.pause_request = request
                save(self.paths.run / 'trusted/pause-request.json', request)
                return
            except (OSError, ValueError, TypeError, AttributeError) as error:
                self.trigger_error = type(error).__name__
                return

    def worker(self, attempt, remaining, pause):
        name = self.pins['worker_names'][attempt]
        directory = self.paths.run / 'attempts' / attempt
        config = self.paths.fixture / 'attempts' / attempt
        spec = ['docker', 'create', '--name', name, '--platform', 'linux/arm64',
            '--label', 'com.docker.compose.project=harness-lab',
            '--label', 'com.docker.compose.service=' + self.service,
            '--label', 'org.harness-lab.role=worker',
            '--label', 'org.harness-lab.chain=' + self.args.chain_id,
            '--network', 'harness-lab_lab', '--read-only', '--memory', '2g', '--cpus', '1',
            '--pids-limit', '256', '--tmpfs', '/tmp:size=128m',
            '-e', 'MODEL_API_BASE_URL=http://model-proxy:8082/v1',
            '-e', 'MODEL_NAME=' + self.pins['model_name'],
            '-e', 'MODEL_RUN_TOKEN_FILE=/config/model-run.token']
        mounts = [(config / 'request.json', '/config/request.json', True),
            (config / 'lifecycle.json', '/config/lifecycle.json', True),
            (self.paths.fixture / 'scope.json', '/config/scope.json', True),
            (self.paths.fixture / 'scope.token', '/config/scope.token', True),
            (self.paths.budget / 'run.token', '/config/model-run.token', True),
            (self.paths.state, '/state', False), (self.paths.workspace, '/workspace', False),
            (directory / 'candidate', '/evidence', False)]
        if self.args.candidate == 'deepseek_harness':
            spec += ['-e', 'HARNESS_LIFECYCLE_IMAGE_ID=' + self.pins['candidate_image_id']]
            spec += ['-e', 'MODEL_RUN_TOKEN_FILE=/budget/run.token']
            mounts = [m for m in mounts if m[1] != '/config/model-run.token']
            mounts += [(self.paths.budget / 'run.token', '/budget/run.token', True),
                (config / 'lifecycle-state.json', '/config/lifecycle-state.json', True)]
            if attempt == 'worker-b':
                mounts.append((config / 'lifecycle-pause.json', '/config/lifecycle-pause.json', True))
            spec += ['--entrypoint', 'node']
        for source, destination, readonly in mounts:
            if not source.exists() or source.is_symlink():
                raise RuntimeError('显式 Worker 挂载缺失或不安全')
            spec += ['--mount', f'type=bind,src={source},dst={destination}' + (',readonly' if readonly else '')]
        spec.append(self.pins['candidate_image_id'])
        if self.args.candidate == 'deepseek_harness':
            spec += ['--import', 'tsx/esm', '/opt/materials/harness-lab/goal-runtime-entrypoint.ts']
        spec += ['--request', '/config/request.json']
        started, tick = now(), time.monotonic()
        self.names.append(name)
        identifier_ = self.call(spec, capture=True, timeout=30).stdout.strip()
        before = self.snapshot(identifier_)
        if (before is None or before['id'] != identifier_ or before['name'] != '/' + name
                or before['image_id'] != self.pins['candidate_image_id']
                or before['state']['Status'] != 'created' or before['state']['Running']
                or before['memory_limit'] != 2 * 1024 ** 3 or before['cpu_nano'] != 10 ** 9):
            raise RuntimeError('创建的 Worker 与可信启动固定值不同')
        actual_mounts = {m['Destination']: m for m in before['mounts']}
        if set(actual_mounts) != {destination for _, destination, _ in mounts} or any(
                actual_mounts[destination]['Source'] != str(source)
                or actual_mounts[destination]['RW'] is readonly for source, destination, readonly in mounts):
            raise RuntimeError('创建的 Worker 挂载与可信启动固定值不同')
        record = {**self.identity(attempt), 'before_start': before, 'started_at': started}
        save(self.paths.run / 'trusted' / (attempt + '-created.json'), record)
        stop = threading.Event()
        control = None
        timed_out = False
        available = remaining - (time.monotonic() - tick)
        if available <= 0:
            raise TimeoutError('Worker 启动前累计活跃预算已用尽')
        self.sampling_phase = attempt + '_active'
        try:
            with (directory / 'candidate.log').open('x') as log:
                if pause:
                    control = threading.Thread(target=self.pause_trigger, args=(identifier_, attempt, stop), daemon=True)
                    control.start()
                try:
                    process = subprocess.run(['docker', 'start', '--attach', identifier_], cwd=ROOT,
                        env=self.env, text=True, stdout=log, stderr=subprocess.STDOUT,
                        timeout=available)
                    record['attach_exit'] = process.returncode
                except subprocess.TimeoutExpired:
                    timed_out = True
                    self.call(['docker', 'kill', identifier_], capture=True, check=False, timeout=10)
        finally:
            stop.set()
            if control:
                control.join(timeout=12)
                if control.is_alive():
                    raise RuntimeError('暂停触发器未结束')
        after = self.snapshot(identifier_)
        record.update({'after_exit': after, 'process_exit': after['state']['ExitCode'],
            'finished_at': now(), 'active_seconds': time.monotonic() - tick,
            'pause_request': self.pause_request if pause else None, 'deleted': None,
            'errors': (['host_timeout'] if timed_out else []) + ([self.trigger_error] if self.trigger_error else [])})
        save(self.paths.run / 'trusted' / (attempt + '.json'), record)
        self.used_active += record['active_seconds']
        return record

    def run_chain(self, args):
        self.args = args
        if self.containers():
            raise RuntimeError('当前实验容器仍存在；请先完成清理')
        self.config.initialize_layout()
        self.paths = self.config.paths_for('pause-recovery', args.chain_id, args.candidate,
            'alice', 'main', args.chain_id, case='tools')
        if any(path.exists() or path.is_symlink() for path in vars(self.paths).values()):
            raise RuntimeError('链 ID 已使用；不能覆盖任何证据')
        self.day = self.paths.run.parent.parent.parent.name
        self.scope = {'experiment_id': 'pause-recovery', 'variant': args.chain_id,
            'user_id': 'alice', 'session_id': 'main'}
        self.service = CANDIDATES[args.candidate][0]
        self.env.update({'LAB_EXPERIMENT_ID': 'pause-recovery', 'LAB_VARIANT': args.chain_id,
            'LAB_EVIDENCE_HOST': str(self.paths.run), 'LAB_WORKSPACE_HOST': str(self.paths.workspace),
            'LAB_STATE_HOST': str(self.paths.state), 'LAB_BUDGET_HOST': str(self.paths.budget),
            'LAB_FIXTURE_HOST': str(self.paths.fixture), 'LAB_MAX_MODEL_CALLS': '20'})
        resolved = json.loads(self.compose('--profile', 'model', '--profile', self.service,
            'config', '--format', 'json', capture=True).stdout)
        model_name = resolved['services']['model-proxy']['environment'].get('MODEL_NAME')
        image_tag = args.image_tag
        del resolved
        if not isinstance(model_name, str) or not model_name:
            raise RuntimeError('缺少所选模型名称')
        self.env['LAB_MODEL_NAME'] = model_name
        context = args.materials_context.resolve()
        if 'old' in context.parts or not context.is_relative_to(ROOT / 'external/build-context'):
            raise RuntimeError('生命周期材料必须属于当前构建上下文')
        source = json.loads((context / 'SOURCE_MANIFEST.json').read_text())
        adapter = json.loads((context / 'LAB_ADAPTER_MANIFEST.json').read_text())
        image_id = self.call(['docker', 'image', 'inspect', '--format', '{{.Id}}', image_tag], capture=True).stdout.strip()
        bench_id = self.call(['docker', 'image', 'inspect', '--format', '{{.Id}}', 'harness-lab-bench:local'], capture=True).stdout.strip()
        self.pins = {'schema': 'harness-lab-pause-pins/1', 'candidate': args.candidate,
            'chain_id': args.chain_id, 'scope': self.scope, 'model_name': model_name,
            'seed': args.seed, 'date': self.day, 'variant': args.variant,
            'candidate_image_id': image_id, 'candidate_image_tag': image_tag, 'bench_image_id': bench_id,
            'source_export_sha256': source['export_sha256'], 'adapter_export_sha256': adapter['export_sha256'],
            'worker_names': {key: 'harness-lab-pause-' + args.chain_id + '-' + key for key in ('worker-a', 'worker-b')},
            'model_call_cap': 20, 'active_limit_seconds': 300, 'pause_wait_seconds': 600,
            'max_goal_rounds': 20, 'adapter_helper_sha256': hashlib.sha256((ROOT / 'adapters/deepagents_langgraph/pause_recovery.py').read_bytes()).hexdigest() if args.candidate == 'deepagents_langgraph' else None,
            'tool_surface_sha256': None}
        self.candidate_image_ids = {image_id}
        for other, (other_service, _, _, _) in CANDIDATES.items():
            tag = 'harness-lab-' + other_service + ':local'
            reply = self.call(['docker', 'image', 'inspect', '--format', '{{.Id}}', tag], capture=True, check=False, timeout=10)
            if reply.returncode == 0:
                self.candidate_image_ids.add(reply.stdout.strip())
        self.paths.fixture.mkdir(parents=True, exist_ok=False)
        # Compose 服务也使用固定 ID 执行，包括代理重建。
        service_images = {}
        for name in ('fixtures', 'tools', 'model-proxy'):
            service_images[name] = self.call(['docker', 'image', 'inspect', '--format', '{{.Id}}',
                'harness-lab-services:local'], capture=True).stdout.strip()
        override = self.paths.fixture / 'services.compose.json'
        save(override, {'services': {name: {'image': image} for name, image in service_images.items()}})
        self.prefix += ['-f', str(override)]
        self.service_images = service_images

        self.names, self.pause_request, self.trigger_error, self.used_active = [], None, None, 0.0
        samples, stop = [], threading.Event()
        self.samples = samples
        self.sampling_lock = threading.RLock()
        sampler = None
        outcome = {'schema': 'harness-lab-pause-orchestration/1', 'chain_id': args.chain_id,
            'candidate': args.candidate, 'variant': args.variant, 'started_at': now(), 'errors': []}
        try:
            self.compose('up', '-d', '--no-build', 'fixtures')
            self.wait_health('fixtures', 8080)
            self.bench('prepare')
            host_source = Path(__file__).read_bytes()
            (self.paths.run / 'trusted/host-program.py').write_bytes(host_source)
            save(self.paths.run / 'trusted/host-program.json', {
                'sha256': hashlib.sha256(host_source).hexdigest(), 'path': str(Path(__file__).resolve()),
                'purpose': '宿主仅编排 Docker；候选和评分器均未在宿主执行'})
            save(self.paths.run / 'trusted/pins.json', self.pins)
            save(self.paths.run / 'trusted/service-images.json', self.service_images)
            self.compose('up', '-d', '--no-build', 'tools', 'model-proxy')
            self.wait_health('tools', 8081)
            self.wait_health('model-proxy', 8082)
            if args.candidate == 'deepseek_harness':
                self.calibrate_surface()
                save(self.paths.run / 'trusted/pins.json', self.pins)
            self.bench('bind-start', host_record=self.paths.run / 'trusted/pins.json')
            self.sampling_phase = 'services_idle'
            sampler = threading.Thread(target=self.sample, args=(stop, samples, self.paths.run), daemon=True)
            sampler.start()
            a = self.worker('worker-a', 300, args.variant == 'requested-pause')
            if args.variant == 'requested-pause':
                # 不在采样器读取清单与检查确切 ID 退出状态之间
                # 删除对象；删除本身也要留证。
                with self.sampling_lock:
                    self.call(['docker', 'rm', a['after_exit']['id']], capture=True, timeout=20)
                    a['deleted'] = {'id_absent': self.snapshot(a['after_exit']['id']) is None,
                        'name_absent': self.snapshot(self.pins['worker_names']['worker-a']) is None, 'observed_at': now()}
                save(self.paths.run / 'trusted/worker-a.json', a)
                self.bench('check-paused', host_record=self.paths.run / 'trusted/worker-a.json')
                self.wait_after_delete(a)
                self.bench('bind-resume', host_record=self.paths.run / 'trusted/wait.json')
                remaining = math.floor(300 - self.used_active)
                if remaining < 1:
                    raise RuntimeError('累计活跃预算已用尽')
                b = self.worker('worker-b', remaining, False)
                self.halt(names=self.names, services=('model-proxy',))
                result = self.bench('grade', host_record=self.paths.run / 'trusted/worker-b.json', check=False)
                outcome['grade_exit'] = result.returncode
                return result.returncode
            self.halt(names=self.names, services=('model-proxy',))
            result = self.bench('grade', host_record=self.paths.run / 'trusted/worker-a.json', check=False)
            outcome['grade_exit'] = result.returncode
            return result.returncode
        except Exception as error:
            outcome['errors'].append(type(error).__name__)
            raise
        finally:
            try:
                self.halt(names=self.names, services=('model-proxy',))
            finally:
                stop.set()
                if sampler:
                    sampler.join(timeout=10)
            try:
                if self.paths.run.exists():
                    for service in ('fixtures', 'tools', 'model-proxy'):
                        log = self.compose('logs', '--no-color', service, capture=True, check=False, timeout=10)
                        (self.paths.run / (service + '.log')).write_text(log.stdout + log.stderr)
                    save(self.paths.run / 'orchestration.json', {**outcome, 'finished_at': now(), 'used_active_seconds': self.used_active})
                    save(self.paths.run / 'resource-metrics.json', summarize([{k: v for k, v in sample.items() if k != 'error'} if sample.get('error') is None else sample for sample in samples]))
            finally:
                self.cleanup(('model-proxy', 'tools', 'fixtures'), names=self.names)
                remaining_containers = self.containers()
                destination = self.paths.run if self.paths.run.is_dir() else self.paths.fixture
                save(destination / 'cleanup.json', {'finished_at': now(), 'remaining': remaining_containers,
                    'passed': not remaining_containers})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', choices=('deepagents_langgraph', 'deepseek_harness'), required=True)
    parser.add_argument('--chain-id', type=identifier, required=True)
    parser.add_argument('--seed', type=int, choices=(101, 202, 303, 404, 505), required=True)
    parser.add_argument('--variant', choices=('uninterrupted', 'requested-pause'), required=True)
    parser.add_argument('--restart-proxy', action='store_true')
    parser.add_argument('--image-tag', required=True)
    parser.add_argument('--materials-context', type=Path, required=True)
    args = parser.parse_args()
    # 现有批次锁使本实验与全部详细批次串行。
    lab = PauseLab()
    lock_path = lab.config.host_data_root / 'runs/.detailed-batch.lock'
    if lock_path.is_symlink():
        raise RuntimeError('串行执行锁不能跟随链接')
    with lock_path.open('a') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return lab.run_chain(args)


if __name__ == '__main__':
    raise SystemExit(main())
