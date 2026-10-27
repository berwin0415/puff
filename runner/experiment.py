"""在容器内准备候选框架工具运行，并独立判定。"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import sqlite3

import httpx

from runner.bench import environment, identifier
from runner.config import load_config
from runner.grading import grade_tools, prepare_tools_inputs

PROFILES = {'deepseek_harness': 'sdk-minimal', 'openclaw': 'gateway-http', 'deepagents_langgraph': 'create-deep-agent', 'pi_agent_core': 'agent-core', 'google_adk': 'adk-runner', 'openai_agents_sdk': 'agents-runner', 'openhands': 'local-conversation', 'codex_sdk': 'codex-sdk-mcp'}

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def context(args):
    config = load_config().for_environment('container')
    paths = config.paths_for('screening', args.run_id, args.candidate, 'alice', 'main', args.run_id, case='tools', date=args.date)
    scope = {'experiment_id': 'screening', 'variant': args.run_id, 'user_id': 'alice', 'session_id': 'main'}
    return config, paths, scope


def ledger_request(paths, method, path, **kwargs):
    token = (paths.fixture / '.admin-token').read_text().strip()
    with httpx.Client(timeout=10, trust_env=False) as client:
        response = client.request(method, 'http://fixtures:8080' + path,
                                  headers={'Authorization': 'Bearer ' + token}, **kwargs)
        response.raise_for_status()
        return response.json()


def prepare(args):
    config, paths, scope = context(args)
    paths.run.mkdir(parents=True, exist_ok=False)
    for directory in (paths.state, paths.workspace, paths.budget, paths.run / 'candidate'):
        directory.mkdir(parents=True, exist_ok=False)
    setup = ledger_request(paths, 'POST', '/admin/scenarios', json={
        'experiment_id': scope['experiment_id'], 'variant': scope['variant'], 'seed': args.seed,
        'users': [{'user_id': scope['user_id'], 'session_id': scope['session_id']}], 'write_mode': 'idempotent'})
    scope_token = setup['scopes'][0]['token']
    for path, token in ((paths.budget / 'run.token', secrets.token_urlsafe(32)), (paths.fixture / 'scope.token', scope_token)):
        path.write_text(token + '\n')
        path.chmod(0o600)
    save(paths.fixture / 'tool-binding.json', {
        'fixture_url': 'http://fixtures:8080', 'fixture_token': scope_token, 'scope': scope,
        'workspace': '/workspace', 'events_path': '/evidence/tool-events.jsonl'})
    public = prepare_tools_inputs(paths.workspace, scope)
    prompt = '本任务可信作用域为 ' + json.dumps(scope, ensure_ascii=False) + '。\n' + public['prompt']
    save(paths.fixture / 'candidate-request.json', {
        'run_id': args.run_id, 'session_id': scope['session_id'], 'prompt': prompt,
        'workspace': '/workspace', 'state_dir': '/state', 'output_dir': '/evidence',
        'tools_url': 'http://tools:8081', 'tool_token_file': '/config/scope.token',
        'max_output_tokens': 4096, 'timeout_seconds': config.budget.active_timeout_seconds,
        'max_retries': config.budget.max_retries})
    save(paths.run / 'input.json', {'candidate': args.candidate, 'run_id': args.run_id, 'case': 'tools',
                                  'seed': args.seed, 'scope': scope, 'prompt': prompt,
                                  'model_call_cap': config.budget.max_model_calls,
                                  'active_timeout_seconds': config.budget.active_timeout_seconds,
                                  'expected_answer_delivered': False, 'profile': PROFILES[args.candidate]})
    save(paths.run / 'bench-environment.json', environment())
    print(json.dumps({'prepared': True, 'run_id': args.run_id, 'expected_answer_delivered': False}))


def grade(args):
    config, paths, scope = context(args)
    ledger = ledger_request(paths, 'GET', '/admin/ledger', params={'experiment_id': scope['experiment_id'], 'variant': scope['variant']})
    save(paths.run / 'ledger.json', ledger)
    event_file = paths.run / 'tool-events.jsonl'
    events = [json.loads(line) for line in event_file.read_text().splitlines()] if event_file.exists() else []
    result = grade_tools(args.seed, scope, paths.workspace, ledger, events)
    result.update({'run_id': args.run_id, 'candidate': args.candidate, 'case': 'tools',
                   'scope': '仅一次初筛运行，尚非详细多种子比较'})
    database = paths.budget / 'calls.sqlite'
    if database.exists():
        with sqlite3.connect(f'file:{database}?mode=ro', uri=True) as db:
            saved_cap = db.execute('SELECT cap FROM budget WHERE id=1').fetchone()
            calls = [dict(zip(('id', 'started_at', 'path', 'outcome', 'upstream_status'), row))
                     for row in db.execute('SELECT id,started_at,path,outcome,upstream_status FROM calls ORDER BY id')]
        save(paths.run / 'model-calls.json', calls)
        result['metrics']['model_requests'] = len(calls)
        result['metrics']['enforced_model_call_cap'] = config.budget.max_model_calls
        model_checks = {
            'model_calls_within_cap': 1 <= len(calls) <= config.budget.max_model_calls,
            'real_upstream_success': any(c['outcome'] == 'forwarded' and 200 <= (c['upstream_status'] or 0) < 300 for c in calls),
            'model_cap_matches_config': saved_cap == (config.budget.max_model_calls,),
        }
        result['checks'].extend({'name': name, 'passed': passed, 'detail': '独立持久化代理账本'}
                                for name, passed in model_checks.items())
        result['passed'] = result['passed'] and all(model_checks.values())
    else:
        result['passed'] = False
        result['checks'].append({'name': 'model_counter_exists', 'passed': False, 'detail': '未创建模型预算数据库'})
    result['metrics'].update({'model_cost_usd': None, 'price_source': None})
    save(paths.run / 'verdict.json', result)
    print(json.dumps(result))
    return 0 if result['passed'] else 1


def main():
    environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'grade'))
    parser.add_argument('--run-id', required=True, type=identifier)
    parser.add_argument('--candidate', choices=tuple(PROFILES), required=True)
    parser.add_argument('--seed', type=int, choices=(101, 202, 303, 404, 505), required=True)
    parser.add_argument('--date', required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args)
        return 0
    return grade(args)


if __name__ == '__main__':
    raise SystemExit(main())
