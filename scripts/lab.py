"""宿主仅编排 Docker；Agent 执行和评分均在镜像内进行。"""
from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.config import load_config
from runner.bench import identifier
from runner.metrics import summarize

CANDIDATES = {
    'deepseek_harness': ('deepseek-harness', 'dsh', 'sdk-minimal', 'I08'),
    'openclaw': ('openclaw', 'openclaw', 'gateway-http', 'I09'),
    'deepagents_langgraph': ('deepagents-langgraph', 'deepagents', 'create-deep-agent', 'I10'),
    'pi_agent_core': ('pi-agent-core', 'pi', 'agent-core', 'I11'),
    'google_adk': ('google-adk', 'adk', 'adk-runner', 'I12'),
    'openai_agents_sdk': ('openai-agents-sdk', 'agents-sdk', 'agents-runner', 'I13'),
    'openhands': ('openhands', 'openhands', 'local-conversation', 'I15'),
    'codex_sdk': ('codex-sdk', 'codex', 'codex-sdk-mcp', 'I14'),
}


class Lab:
    def __init__(self):
        self.config = load_config(ROOT / 'configs/experiment.yaml')
        self.env = {**os.environ, 'HARNESS_LAB_DATA_ROOT': str(self.config.host_data_root)}
        self.prefix = ['docker', 'compose', '--project-name', 'harness-lab', '-f', str(ROOT / 'compose/docker-compose.yml')]

    def call(self, args, *, capture=False, check=True, timeout=None):
        return subprocess.run(args, cwd=ROOT, env=self.env, text=True, capture_output=capture, check=check, timeout=timeout)

    def compose(self, *args, **kwargs):
        return self.call(self.prefix + list(args), **kwargs)

    def halt(self, names=(), services=()):
        for name in names:
            try:
                self.call(['docker', 'kill', name], capture=True, check=False, timeout=10)
            except Exception:
                pass
        for service in services:
            try:
                self.compose('stop', '--timeout', '3', service, capture=True, check=False, timeout=10)
            except Exception:
                pass

    def cleanup(self, services, names=()):
        failures = []
        for name in names:
            try:
                result = self.call(['docker', 'rm', '-f', name], capture=True, check=False, timeout=10)
                if result.returncode and 'No such container' not in result.stderr:
                    failures.append(name)
            except Exception:
                failures.append(name)
        for service in services:
            for args in [('stop', '--timeout', '3', service), ('rm', '-f', service)]:
                try:
                    result = self.compose(*args, capture=True, check=False, timeout=10)
                    if result.returncode:
                        failures.append(service)
                except Exception:
                    failures.append(service)
        if failures:
            print('清理时无法通过 Docker 确认以下对象：' + ', '.join(sorted(set(failures))), file=sys.stderr)

    def containers(self):
        result = self.call(['docker', 'ps', '-aq', '--filter', 'label=com.docker.compose.project=harness-lab'], capture=True, timeout=5)
        ids = result.stdout.split()
        if not ids:
            return []
        data = json.loads(self.call(['docker', 'inspect', *ids], capture=True, timeout=5).stdout)
        # 不序列化 Config.Env：代理持有上游凭证。
        return [{'id': c['Id'], 'name': c['Name'], 'image_id': c['Image'],
                 'image_tag': c['Config']['Image'], 'state': c['State'],
                 'memory_limit': c['HostConfig']['Memory'], 'cpu_nano': c['HostConfig']['NanoCpus'],
                 'mounts': [{k: m[k] for k in ('Source', 'Destination', 'RW')} for m in c['Mounts']]}
                for c in data]

    def preflight(self):
        self.config.initialize_layout()
        self.compose('config', '--quiet')
        info = json.loads(self.call(['docker', 'info', '--format', '{{json .}}'], capture=True).stdout)
        print(json.dumps({'docker_architecture': info['Architecture'], 'docker_memory': info['MemTotal'],
                          'docker_cpus': info['NCPU'], 'data_root': str(self.config.host_data_root),
                          'configured_memory': self.config.resources.memory_bytes,
                          'configured_cpus': self.config.resources.cpus,
                          'model_call_cap': self.config.budget.max_model_calls,
                          'model_cost_enforced': False, 'model_called': False}, indent=2))
        if info['OSType'] != 'linux' or info['MemTotal'] < self.config.resources.memory_bytes:
            raise RuntimeError('Docker 环境未达到配置的资源预算')

    def infrastructure(self, run_id):
        if any(c['state']['Running'] for c in self.containers()):
            raise RuntimeError('已有 harness-lab 容器运行，请先结束该轮实验')
        date = datetime.now(ZoneInfo('Asia/Shanghai')).date().isoformat()
        paths = self.config.paths_for('infrastructure', run_id, 'infrastructure', 'alice', 'main', run_id, case='browser', date=date)
        evidence = paths.run
        if evidence.exists():
            raise RuntimeError('运行 ID 已存在；请使用新 ID，不要覆盖证据')
        self.env.update({'LAB_EXPERIMENT_ID': 'infrastructure', 'LAB_VARIANT': run_id,
                         'LAB_EVIDENCE_HOST': str(evidence),
                         'LAB_WORKSPACE_HOST': str(paths.workspace)})
        fixture = paths.fixture
        fixture.mkdir(parents=True, exist_ok=False)
        samples, errors = [], []
        event = threading.Event()
        sampler = None
        active_ids = []
        passed = False
        probe_name = 'harness-lab-browser-probe-' + run_id
        try:
            self.compose('up', '-d', '--no-build', 'fixtures')
            self.wait_health('fixtures', 8080)
            self.compose('run', '--rm', '--entrypoint', 'python', 'bench', '-m', 'runner.infrastructure', 'prepare', '--run-id', run_id, '--date', date)
            sampler = threading.Thread(target=self.sample, args=(event, samples, evidence), daemon=True)
            sampler.start()
            self.compose('up', '-d', '--no-build', 'tools', 'site', 'browser')
            self.wait_health('tools', 8081)
            self.wait_health('site', 8081)
            self.compose('run', '--name', probe_name, '--entrypoint', 'node', '-v',
                         str(fixture / 'browser-probe.json') + ':/input/browser-probe.json:ro',
                         'browser', '/opt/browser/acceptance.mjs', timeout=120)
            active_ids = self.containers()
            # MCP 启动自身浏览器前，先删除基础 Chromium 进程。
            self.call(['docker', 'rm', probe_name])
            self.compose('run', '--rm', '--entrypoint', 'python', 'bench', '-m', 'runner.infrastructure', 'verify', '--run-id', run_id, '--date', date)
            passed = True
        except Exception as exc:
            errors.append(type(exc).__name__ + '：基础设施命令失败；请检查已保存的服务日志')
            raise
        finally:
            self.halt(names=(probe_name,))
            event.set()
            if sampler is not None:
                sampler.join(timeout=10)
            try:
                if evidence.exists():
                    services = ('fixtures', 'tools', 'site', 'browser')
                    for service in services:
                        log = self.compose('logs', '--no-color', service, capture=True, check=False, timeout=10)
                        (evidence / (service + '.log')).write_text(log.stdout + log.stderr)
                    snapshot = self.containers()
                    (evidence / 'manifest.json').write_text(json.dumps({'run_id': run_id, 'passed': passed,
                        'candidate_executed': False, 'model_called': False, 'containers': active_ids or snapshot,
                        'final_containers': snapshot, 'errors': errors}, indent=2) + '\n')
                    (evidence / 'metrics.json').write_text(json.dumps(summarize(samples), indent=2) + '\n')
            finally:
                self.cleanup(('browser', 'site', 'tools', 'fixtures'), names=(probe_name,))

    def wait_health(self, service, port):
        # HTTP 请求本身在已构建的 bench 镜像内执行。
        code = ('import httpx,time\n'
                'for i in range(30):\n'
                ' try:\n'
                f'  r=httpx.get("http://{service}:{port}/health",timeout=1,trust_env=False); r.raise_for_status(); break\n'
                ' except httpx.HTTPError: time.sleep(0.3)\n'
                'else: raise SystemExit("服务健康检查超时")\n')
        self.compose('run', '--rm', '--entrypoint', 'python', 'bench', '-c', code)

    def run_candidate(self, candidate, run_id, seed):
        if any(c['state']['Running'] for c in self.containers()):
            raise RuntimeError('另一项实验仍在运行')
        paths = self.config.paths_for('screening', run_id, candidate, 'alice', 'main', run_id, case='tools')
        if paths.run.exists() or paths.fixture.exists():
            raise RuntimeError('运行 ID 已存在；请使用新 ID，不要覆盖证据')
        # Compose 解析 env-file 引号。结果仅留在内存，不写日志。
        resolved = json.loads(self.compose('--profile', 'model', 'config', '--format', 'json', capture=True).stdout)
        model = resolved['services']['model-proxy']['environment'].get('MODEL_NAME')
        if not model or not isinstance(model, str):
            raise RuntimeError('缺少所选的 MODEL_NAME')
        del resolved
        self.env.update({'LAB_EXPERIMENT_ID': 'screening', 'LAB_VARIANT': run_id,
                         'LAB_EVIDENCE_HOST': str(paths.run), 'LAB_WORKSPACE_HOST': str(paths.workspace),
                         'LAB_STATE_HOST': str(paths.state), 'LAB_BUDGET_HOST': str(paths.budget),
                         'LAB_FIXTURE_HOST': str(paths.fixture), 'LAB_MODEL_NAME': model,
                         'LAB_MAX_MODEL_CALLS': str(self.config.budget.max_model_calls)})
        paths.fixture.mkdir(parents=True, exist_ok=False)
        day = paths.run.parent.parent.parent.name
        common = ['--run-id', run_id, '--candidate', candidate, '--seed', str(seed), '--date', day]
        candidate_service, container_prefix, profile, step = CANDIDATES[candidate]
        container_name = 'harness-lab-' + container_prefix + '-' + run_id
        samples, records = [], []
        event = threading.Event()
        sampler = None
        candidate_exit, grade_exit = None, None
        try:
            self.compose('up', '-d', '--no-build', 'fixtures')
            self.wait_health('fixtures', 8080)
            self.compose('run', '--rm', '--entrypoint', 'python', 'bench', '-m', 'runner.experiment', 'prepare', *common)
            self.compose('up', '-d', '--no-build', 'tools', 'model-proxy')
            self.wait_health('tools', 8081)
            self.wait_health('model-proxy', 8082)
            self.sampling_phase = 'services_idle'
            sampler = threading.Thread(target=self.sample, args=(event, samples, paths.run), daemon=True)
            sampler.start()
            time.sleep(self.config.sampling.interval_seconds)
            self.sampling_phase = 'candidate_active'
            try:
                result = self.compose('run', '--name', container_name, candidate_service,
                                      check=False, timeout=self.config.budget.active_timeout_seconds + 30)
                candidate_exit = result.returncode
            except subprocess.TimeoutExpired:
                self.halt(names=(container_name,))
                candidate_exit = 124
            self.sampling_phase = 'grading'
            records = self.containers()
            grade_exit = self.compose('run', '--rm', '--entrypoint', 'python', 'bench', '-m', 'runner.experiment', 'grade', *common, check=False).returncode
        finally:
            # 在任何可能失败的证据 I/O 之前，先停止付费执行。
            self.halt(names=(container_name,), services=('model-proxy',))
            event.set()
            if sampler is not None:
                sampler.join(timeout=10)
            try:
                if paths.run.exists():
                    for service in ('fixtures', 'tools', 'model-proxy'):
                        result = self.compose('logs', '--no-color', service, capture=True, check=False, timeout=10)
                        (paths.run / (service + '.log')).write_text(result.stdout + result.stderr)
                    result = self.call(['docker', 'logs', container_name], capture=True, check=False, timeout=10)
                    (paths.run / 'candidate.log').write_text(result.stdout + result.stderr)
                    manifest = {'run_id': run_id, 'candidate': candidate, 'model_name': model, 'seed': seed,
                                'candidate_exit_code': candidate_exit, 'grade_exit_code': grade_exit,
                                'containers': records or self.containers(), 'execution_profile': profile,
                                'real_model_credential_owner': 'model-proxy only',
                                'candidate_output_mount': '仅候选子目录；可信审计材料保存在外部',
                                'plan_scope': step + ' 单轮初筛；不据此判断暂停或恢复能力'}
                    (paths.run / 'manifest.json').write_text(json.dumps(manifest, indent=2) + '\n')
                    metrics = summarize(samples)
                    metrics['phases'] = {phase: summarize([s for s in samples if s['phase'] == phase])
                                         for phase in sorted({s['phase'] for s in samples})}
                    (paths.run / 'resource-metrics.json').write_text(json.dumps(metrics, indent=2) + '\n')
            finally:
                self.cleanup(('tools', 'model-proxy', 'fixtures'), names=(container_name,))
        return 0 if candidate_exit == grade_exit == 0 else 1

    def sample(self, event, samples, evidence):
        path = evidence / 'resource-samples.jsonl'
        while not event.is_set():
            at = time.time()
            try:
                ids = self.call(['docker', 'ps', '-q', '--filter', 'label=com.docker.compose.project=harness-lab'], capture=True, timeout=5).stdout.split()
                data = self.call(['docker', 'stats', '--no-stream', '--format', '{{json .}}', *ids], capture=True, timeout=5).stdout if ids else ''
                sample = {'unix_time': at, 'phase': getattr(self, 'sampling_phase', 'infrastructure'),
                          'containers': [json.loads(line) for line in data.splitlines()]}
            except Exception:
                sample = {'unix_time': at, 'phase': getattr(self, 'sampling_phase', 'infrastructure'),
                          'containers': [], 'error': 'Docker 采样失败'}
            samples.append(sample)
            with path.open('a') as stream:
                stream.write(json.dumps(sample) + '\n')
            event.wait(max(0, self.config.sampling.interval_seconds - (time.time() - at)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='action', required=True)
    commands.add_parser('preflight')
    build = commands.add_parser('build')
    build.add_argument('--candidate', choices=('infrastructure', *CANDIDATES), default='infrastructure')
    run = commands.add_parser('run')
    run.add_argument('--candidate', choices=tuple(CANDIDATES), required=True)
    run.add_argument('--case', choices=('tools',), required=True)
    run.add_argument('--seed', type=int, choices=(101, 202, 303, 404, 505), required=True)
    run.add_argument('--run-id', required=True, type=identifier)
    check = commands.add_parser('check')
    check.add_argument('--run-id', required=True, type=identifier)
    infrastructure = commands.add_parser('infrastructure')
    infrastructure.add_argument('--run-id', required=True, type=identifier)
    for action in ('pause', 'resume', 'replace-worker'):
        item = commands.add_parser(action)
        item.add_argument('--run-id', required=True, type=identifier)
    args = parser.parse_args()
    lab = Lab()
    if args.action == 'preflight':
        lab.preflight()
    elif args.action == 'build':
        services = ('bench', 'fixtures', 'browser') if args.candidate == 'infrastructure' else (CANDIDATES[args.candidate][0],)
        if args.candidate in CANDIDATES:
            lab.call([sys.executable, 'scripts/stage_' + args.candidate + '.py'])
        for service in services:
            lab.compose('build', service)
    elif args.action == 'check':
        lab.compose('run', '--rm', 'bench', 'check', '--run-id', args.run_id)
    elif args.action == 'infrastructure':
        lab.infrastructure(args.run_id)
    elif args.action == 'run':
        raise SystemExit(lab.run_candidate(args.candidate, args.run_id, args.seed))
    else:
        parser.exit(2, '尚未接通候选控制；未改变任何状态。\n')


if __name__ == '__main__':
    main()
