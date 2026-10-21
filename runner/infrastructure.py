"""Prepare and grade the shared browser infrastructure inside Docker only."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import json
from pathlib import Path
import secrets
from zoneinfo import ZoneInfo

import httpx

from runner.bench import environment, identifier
from runner.config import load_config
from fixtures.data import items_for


def locations(run_id, day):
    fixture = Path('/data/fixtures/infrastructure') / run_id
    evidence = load_config().for_environment('container').paths_for(
        'infrastructure', run_id, 'infrastructure', 'alice', 'main', run_id, case='browser', date=day).run
    return fixture, evidence


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + '\n')


def prepare(run_id, day):
    fixture, evidence = locations(run_id, day)
    evidence.mkdir(parents=True, exist_ok=False)
    (evidence / 'browser').mkdir()
    with httpx.Client(timeout=10, trust_env=False) as client:
        admin = (fixture / '.admin-token').read_text().strip()
        response = client.post('http://fixtures:8080/admin/scenarios', headers={'Authorization': 'Bearer ' + admin}, json={
            'experiment_id': 'infrastructure', 'variant': run_id, 'seed': 101,
            'users': [{'user_id': 'alice', 'session_id': 'main'}], 'write_mode': 'idempotent',
        })
        response.raise_for_status()
        scope = response.json()['scopes'][0]
    password = secrets.token_urlsafe(16)
    save(fixture / 'browser-users.json', {'alice': {'password': password, 'token': scope['token']}})
    save(fixture / 'browser-probe.json', {'url': 'http://site:8081', 'username': 'alice', 'password': password,
                                        'operation_id': 'browser-primitives'})
    workspace = load_config().for_environment('container').paths_for(
        'infrastructure', run_id, 'infrastructure', 'alice', 'main', run_id, case='browser').workspace
    workspace.mkdir(parents=True, exist_ok=False)
    save(fixture / 'tool-binding.json', {
        'fixture_url': 'http://fixtures:8080', 'fixture_token': scope['token'], 'workspace': '/workspace',
        'scope': {key: scope[key] for key in ('experiment_id', 'variant', 'user_id', 'session_id')},
        'events_path': '/evidence/tool-events.jsonl',
    })
    save(evidence / 'environment.json', environment())
    print(json.dumps({'prepared': True, 'run_id': run_id, 'evidence': str(evidence)}))


def mcp_check(client, evidence):
    headers = {'Accept': 'application/json, text/event-stream'}
    records = []

    def rpc(method, params, ident):
        body = {'jsonrpc': '2.0', 'method': method, 'params': params}
        if ident is not None:
            body['id'] = ident
        response = client.post('http://browser:8931/mcp', json=body, headers=headers)
        response.raise_for_status()
        if response.headers.get('mcp-session-id'):
            headers['Mcp-Session-Id'] = response.headers['mcp-session-id']
        if ident is None:
            return None
        if response.headers.get('content-type', '').startswith('text/event-stream'):
            messages = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]
            payload = next(message for message in messages if message.get('id') == ident)
        else:
            payload = response.json()
        records.append({'request': body, 'response': payload})
        if 'error' in payload:
            raise RuntimeError(f'MCP {method} returned an error')
        return payload['result']

    rpc('initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
                       'clientInfo': {'name': 'harness-lab-infrastructure', 'version': '0.1.0'}}, 1)
    rpc('notifications/initialized', {}, None)
    tools = rpc('tools/list', {}, 2)
    names = {tool['name'] for tool in tools['tools']}
    assert {'browser_navigate', 'browser_snapshot', 'browser_click', 'browser_fill_form'} <= names
    result = rpc('tools/call', {'name': 'browser_navigate', 'arguments': {'url': 'http://site:8081/login'}}, 3)
    assert not result.get('isError'), result
    result = rpc('tools/call', {'name': 'browser_snapshot', 'arguments': {}}, 4)
    assert not result.get('isError'), result
    save(evidence / 'mcp-raw.json', records)
    return {'mcp_tool_list': True, 'mcp_navigation': True, 'mcp_snapshot': True}


def verify(run_id, day):
    fixture, evidence = locations(run_id, day)
    primitive = json.loads((evidence / 'browser/primitive-result.json').read_text())
    actions = [json.loads(line) for line in (evidence / 'site-actions.jsonl').read_text().splitlines()]
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.get('http://fixtures:8080/admin/ledger', params={
            'experiment_id': 'infrastructure', 'variant': run_id},
            headers={'Authorization': 'Bearer ' + (fixture / '.admin-token').read_text().strip()})
        response.raise_for_status()
        ledger = response.json()
        save(evidence / 'ledger.json', ledger)
        checks = mcp_check(client, evidence)
    writes = ledger['writes']
    downloaded = list(csv.DictReader((evidence / 'browser/download.csv').open()))
    receipt = json.loads(downloaded[0]['receipt_json']) if len(downloaded) == 1 else {}
    target = items_for(101, 'alice', 'main')[3]
    checks.update({
        'primitive_flow': primitive['passed'] is True,
        'single_actual_write': len(writes) == 1,
        'correct_write': len(writes) == 1 and all(writes[0][k] == primitive[k] for k in ('operation_id', 'item_id', 'quantity'))
                         and writes[0]['item_id'] == target['item_id'] and writes[0]['quantity'] == 2
                         and writes[0]['total_cents'] == target['unit_price_cents'] * 2,
        'correct_user': all(row['user_id'] == 'alice' and row['session_id'] == 'main' for row in writes),
        'login_recorded': any(a['path'] == '/login' and a['method'] == 'POST' and a['status'] == 303 for a in actions),
        'multiple_pages': any(a['path'] == '/items?page=2' and a['status'] == 200 for a in actions),
        'download_recorded': any(a['path'].startswith('/download?') and a['status'] == 200 for a in actions),
        'download_matches_receipt': receipt.get('write_count') == 1 and receipt.get('receipts') == writes,
        'trace_saved': (evidence / 'browser/trace.zip').stat().st_size > 0,
        'client_validation_blocks_write': primitive['invalid_quantity_rejected'] is True,
    })
    verdict = {'run_id': run_id, 'passed': all(checks.values()), 'assertions': checks,
               'scope': 'infrastructure only; no candidate or model executed'}
    save(evidence / 'verdict.json', verdict)
    print(json.dumps(verdict))
    return 0 if verdict['passed'] else 1


def main():
    environment()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('prepare', 'verify'))
    parser.add_argument('--run-id', required=True, type=identifier)
    parser.add_argument('--date', required=True)
    args = parser.parse_args()
    if args.action == 'prepare':
        prepare(args.run_id, args.date)
        return 0
    return verify(args.run_id, args.date)


if __name__ == '__main__':
    raise SystemExit(main())
