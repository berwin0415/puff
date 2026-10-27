"""完整性、身份及来源校准，不调用 SQL 或远程服务。"""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.summarize_token_usage import (
    EvidenceError, FIELDS, I24, I24_BASE, I25, MAX_COUNT, absolute_path, digest,
    locate, merge_fields, normalize_usage, parse_deepagents, parse_dsh,
    parse_openclaw, parse_sdk, read_rows, summarize, summarize_observations,
    within,
)


SCOPE = {'experiment_id': 'detailed', 'variant': 'run-one', 'user_id': 'alice', 'session_id': 'main'}
REF = {'path': 'runs/day/candidate/tools/run-one/candidate/raw-events.jsonl', 'sha256': 'a' * 64, 'line': 1}
WIRE = {'prompt_tokens': 100, 'completion_tokens': 20, 'total_tokens': 120,
        'prompt_tokens_details': {'cached_tokens': 60},
        'completion_tokens_details': {'reasoning_tokens': 10}}
DSH = {'inputTokens': 40, 'outputTokens': 20, 'totalTokens': 120,
       'cacheReadTokens': 60, 'reasoningTokens': 10}
DA = {'input_tokens': 100, 'output_tokens': 20, 'total_tokens': 120,
      'input_token_details': {'cache_read': 60}, 'output_token_details': {'reasoning': 10}}
OC = {'input': 40, 'output': 20, 'cacheRead': 60, 'cacheWrite': 0, 'totalTokens': 120,
      'reasoningTokens': 10, 'usageReport': {'state': 'available'}}


def test_cache_buckets_and_reasoning_do_not_inflate_total():
    for kind, usage in [('dsh', DSH), ('da', DA), ('wire', WIRE), ('oc', OC)]:
        fields = normalize_usage(usage, kind)
        assert fields['prompt_tokens']['value'] == 100
        assert fields['completion_tokens']['value'] == 20
        assert fields['total_tokens']['value'] == 120
        assert fields['cache_read_tokens']['value'] == 60
        assert fields['reasoning_tokens']['value'] == 10
    assert normalize_usage(DSH, 'dsh')['cache_write_tokens'] == {'status': 'unknown', 'value': None}


def test_missing_cache_bucket_does_not_become_zero_or_incomplete_prompt():
    result = normalize_usage({'inputTokens': 40, 'outputTokens': 20, 'cacheReadTokens': 60}, 'dsh')
    assert result['prompt_tokens']['status'] == 'unknown'
    result = normalize_usage({**DSH, 'cacheWriteTokens': 0}, 'dsh')
    assert result['prompt_tokens']['value'] == 100


@pytest.mark.parametrize('kind,usage,key', [('dsh', DSH, 'inputTokens'), ('oc', OC, 'input')])
def test_explicit_null_base_counter_is_not_repaired_from_totals(kind, usage, key):
    result = normalize_usage({**usage, key: None}, kind)
    assert result['prompt_tokens'] == {'status': 'conflict', 'value': None}


@pytest.mark.parametrize('invalid', [-1, 1.5, True, 'SECRET_PAYLOAD', MAX_COUNT + 1, None])
def test_invalid_counter_is_conflict_without_echo(invalid):
    result = normalize_usage({'prompt_tokens': invalid}, 'wire')
    assert result['prompt_tokens'] == {'status': 'conflict', 'value': None}
    assert 'SECRET_PAYLOAD' not in json.dumps(result)


def test_provider_explicit_zero_differs_from_missing_counter():
    zero = normalize_usage({'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0}, 'wire')
    assert zero['prompt_tokens'] == {'status': 'reported', 'value': 0}
    missing = normalize_usage({}, 'wire')
    assert all(v['status'] == 'unknown' and v['value'] is None for v in missing.values())


@pytest.mark.parametrize('change', [
    {'total_tokens': 121},
    {'completion_tokens_details': {'reasoning_tokens': 21}},
    {'prompt_tokens_details': {'cached_tokens': 101}},
    {'prompt_cache_hit_tokens': 59},
])
def test_contradictory_counters_are_not_repaired(change):
    fields = normalize_usage({**WIRE, **change}, 'wire')
    assert any(v['status'] == 'conflict' for v in fields.values())


def dsh(seq, kind, usage=None, *, turn=1, step=1, session='main', **extra):
    data = {'turn': turn, 'step': step}
    if kind == 'assistant/chunk':
        data['chunk'] = {'type': 'usage', 'usage': usage}
    elif usage is not None:
        data['usage'] = usage
    data.update(extra)
    return ({'method': 'session.event', 'params': {'sessionId': session,
             'event': {'seq': seq, 'type': kind, 'data': data}}}, dict(REF, line=seq + 1))


def test_dsh_chunk_final_replay_and_retry_are_distinct_boundaries():
    chunk = dsh(1, 'assistant/chunk', DSH)
    final = dsh(2, 'assistant/message', DSH)
    retry = dsh(3, 'llm/retry-started')
    second = dsh(4, 'assistant/message', DSH)
    items, before, _ = parse_dsh([chunk, final, deepcopy(final), retry, second], 'unit', SCOPE)
    assert before == 4 and len(items) == 2
    assert sum(i['fields']['total_tokens']['value'] for i in items) == 240


def test_dsh_final_replaces_provisional_sample_but_conflict_is_retained():
    early = {'inputTokens': 40, 'outputTokens': 10, 'totalTokens': 110, 'cacheReadTokens': 60}
    items, _, _ = parse_dsh([dsh(1, 'assistant/chunk', early), dsh(2, 'assistant/message', DSH)], 'u', SCOPE)
    assert items[0]['fields']['completion_tokens']['value'] == 20
    malformed = {**early, 'outputTokens': -1}
    items, _, _ = parse_dsh([dsh(1, 'assistant/chunk', malformed), dsh(2, 'assistant/message', DSH)], 'u', SCOPE)
    assert items[0]['fields']['completion_tokens']['status'] == 'conflict'


@pytest.mark.parametrize('explicit_null', [False, True])
def test_dsh_missing_final_usage_retains_unknown_response_and_known_attempt_chunk(explicit_null):
    missing = dsh(1, 'assistant/message')
    if explicit_null: missing[0]['params']['event']['data']['usage'] = None
    items, before, issues = parse_dsh([missing], 'u', SCOPE)
    assert before == 0 and len(items) == 1 and issues['response_without_usage'] == 1
    assert all(value['status'] == 'unknown' for value in items[0]['fields'].values())
    final = dsh(2, 'assistant/message')
    if explicit_null: final[0]['params']['event']['data']['usage'] = None
    items, before, issues = parse_dsh([dsh(1, 'assistant/chunk', DSH), final, deepcopy(final)], 'u', SCOPE)
    assert before == 1 and len(items) == 1 and issues['response_without_usage'] == 1
    assert items[0]['fields']['total_tokens']['value'] == 120
    items, _, _ = parse_dsh([dsh(1, 'assistant/chunk', DSH), final,
        dsh(3, 'llm/retry-started'), dsh(4, 'assistant/message')], 'u', SCOPE)
    result = summarize_observations(items)['total_tokens']
    assert len(items) == 2 and result['known_partial_subtotal'] == 120
    assert result['known_observations'] == 1 and result['unknown_observations'] == 1


def test_dsh_usage_before_failure_is_not_lost_and_compaction_is_separate():
    items, _, _ = parse_dsh([dsh(1, 'assistant/chunk', DSH),
        dsh(2, 'compaction/summary', DSH, llmStreamCall=True)], 'u', SCOPE)
    assert len(items) == 2 and {i['kind'] for i in items} == {'model_response', 'auxiliary_response'}
    items, _, issues = parse_dsh([dsh(1, 'compaction/summary', DSH)], 'u', SCOPE)
    assert all(v['status'] == 'unknown' for v in items[0]['fields'].values())
    assert issues['unproven_compaction_call'] == 1


def test_dsh_event_id_conflict_and_foreign_session_refused():
    with pytest.raises(EvidenceError, match='native_event_identity_conflict'):
        parse_dsh([dsh(1, 'assistant/message', DSH), dsh(1, 'assistant/message', {**DSH, 'reasoningTokens': 0})], 'u', SCOPE)
    with pytest.raises(EvidenceError, match='foreign_native_scope'):
        parse_dsh([dsh(1, 'assistant/message', DSH, session='foreign')], 'u', SCOPE)


def da(message_id='one', usage=DA, *, scope=SCOPE, mode='updates', node='model'):
    return ({'scope': scope, 'stream_mode': mode, 'data': {node: {'messages': [
        {'type': 'ai', 'id': message_id, 'usage_metadata': usage, 'content': 'NEVER_EMIT_REASONING'}]}}}, dict(REF))


def test_deepagents_only_model_updates_exclude_history_and_dedup_resume():
    primary = da()
    history = da('previous', mode='checkpoints')
    input_history = da('previous', node='input')
    items, before, _ = parse_deepagents([history, input_history, primary, deepcopy(primary), da('two')], 'u', SCOPE)
    assert len(items) == 2 and before == 3
    assert 'NEVER_EMIT_REASONING' not in json.dumps(items)


def test_deepagents_scope_identity_usage_conflict_and_missing_usage():
    with pytest.raises(EvidenceError, match='unbound_or_foreign_native_scope'):
        parse_deepagents([da(scope=None)], 'u', SCOPE)
    invalid = da(); invalid[0]['data']['model']['messages'][0]['id'] = None
    with pytest.raises(EvidenceError, match='missing_native_response_identity'):
        parse_deepagents([invalid], 'u', SCOPE)
    items, _, _ = parse_deepagents([da(), da(usage={**DA, 'output_token_details': {'reasoning': 9}})], 'u', SCOPE)
    assert items[0]['fields']['reasoning_tokens']['status'] == 'conflict'
    items, _, issues = parse_deepagents([da(usage=None)], 'u', SCOPE)
    assert items[0]['fields']['prompt_tokens']['value'] is None and issues['response_without_usage'] == 1


def oc(messages, *, type_='agent_end', scope=SCOPE):
    session = 'agent:lab-a:openai-user:lab-' + hashlib.sha256(json.dumps(
        [scope['variant'], scope['session_id']], separators=(',', ':')).encode()).hexdigest()
    return ({'type': type_, 'context': {'agentId': 'lab-a', 'sessionKey': session, 'runId': 'native-run'},
        'event': {'runId': 'native-run', 'messages': messages, 'usage': {'total_tokens': 999999}}}, dict(REF))


def assistant(response='response-one', usage=OC):
    return {'role': 'assistant', 'responseId': response, 'usage': usage, 'content': 'NEVER_EMIT_REASONING'}


def test_openclaw_aggregate_and_last_assistant_do_not_double_count():
    row = oc([assistant(), assistant('response-two')])
    row[0]['event']['lastAssistant'] = assistant()
    items, before, _ = parse_openclaw([oc([], type_='llm_output'), row, deepcopy(row)], 'u', SCOPE)
    assert before == 4 and len(items) == 2
    assert sum(i['fields']['total_tokens']['value'] for i in items) == 240
    assert 'NEVER_EMIT_REASONING' not in json.dumps(items)


def test_openclaw_default_zero_unavailable_and_missing_id_are_unknown():
    default = {'input': 0, 'output': 0, 'cacheRead': 0, 'cacheWrite': 0, 'totalTokens': 0}
    items, before, _ = parse_openclaw([oc([assistant(usage=default), assistant('two', {**OC, 'usageReport': {'state': 'unavailable'}}), assistant(None)])], 'u', SCOPE)
    assert before == 0 and len(items) == 3
    assert all(f['value'] is None for item in items for f in item['fields'].values())
    reported = {**default, 'usageReport': {'state': 'available'}}
    items, _, _ = parse_openclaw([oc([assistant(usage=reported)])], 'u', SCOPE)
    assert items[0]['fields']['total_tokens'] == {'value': 0, 'status': 'reported'}


def test_openclaw_unavailable_replays_and_later_measurement_share_response_identity():
    unavailable = oc([assistant(usage={**OC, 'usageReport': {'state': 'unavailable'}})])
    replay = deepcopy(unavailable); replay[1]['line'] = 2
    items, before, _ = parse_openclaw([unavailable, replay], 'u', SCOPE)
    assert before == 0 and len(items) == 1
    assert items[0]['fields']['total_tokens']['value'] is None
    measured = oc([assistant()]); measured[1]['line'] = 3
    for rows in ([unavailable, measured], [measured, unavailable]):
        items, before, _ = parse_openclaw(rows, 'u', SCOPE)
        assert before == 1 and len(items) == 1 and items[0]['kind'] == 'model_response'
        assert items[0]['fields']['total_tokens']['value'] == 120
        assert summarize_observations(items)['total_tokens']['unknown_observations'] == 0


def sdk(kind, event, *, line=1, scope=SCOPE):
    return ({'type': kind, 'scope': scope, 'event': event}, dict(REF, line=line))


def http(attempt=1, **extra):
    return sdk('provider_http_response', {'attempt': attempt, 'status': 200, 'sse': True, **extra})


def wire(attempt=1, response='provider-one', usage=WIRE, **extra):
    return sdk('provider_wire_event', {'attempt': attempt, 'data': {'id': response, 'usage': usage, 'choices': [{'content': 'NEVER_EMIT_REASONING'}], **extra}}, line=2)


def test_sdk_wire_attempts_own_identity_and_repeat_usage_is_not_added():
    native_fake = sdk('llm_end', {'response': {'response_id': '__fake__', 'usage': {'input_tokens': 999999}}})
    items, before, _ = parse_sdk([http(), wire(), wire(), native_fake, http(2), wire(2, 'provider-two')], 'u', SCOPE)
    assert len(items) == 2 and before == 3
    assert sum(i['fields']['total_tokens']['value'] for i in items) == 240
    assert 'NEVER_EMIT_REASONING' not in json.dumps(items)


def test_sdk_missing_usage_id_or_successful_transport_are_not_assumed():
    items, _, issues = parse_sdk([http(), wire(usage=None)], 'u', SCOPE)
    assert len(items) == 1 and issues['response_without_usage'] == 1
    assert items[0]['fields']['total_tokens']['value'] is None
    items, _, issues = parse_sdk([http(), wire(response=None)], 'u', SCOPE)
    assert items[0]['fields']['prompt_tokens']['value'] is None
    assert issues['missing_or_conflicting_wire_response_identity'] == 1
    items, _, issues = parse_sdk([http(status=429)], 'u', SCOPE)
    assert items == [] and issues['transport_without_supported_successful_sse'] == 1
    items, before, issues = parse_sdk([http(sse=False)], 'u', SCOPE)
    assert before == 0 and len(items) == 1 and items[0]['kind'] == 'transport_response_unknown'
    assert issues['transport_without_supported_successful_sse'] == 1
    assert all(value['status'] == 'unknown' for value in items[0]['fields'].values())
    with pytest.raises(EvidenceError, match='wire_without_transport_response'):
        parse_sdk([wire()], 'u', SCOPE)
    with pytest.raises(EvidenceError, match='unbound_or_foreign_native_scope'):
        parse_sdk([sdk('provider_http_response', {'attempt': 1, 'status': 200, 'sse': True}, scope=None)], 'u', SCOPE)


def test_sdk_conflicting_wire_counters_remain_conflict():
    items, _, _ = parse_sdk([http(), wire(), wire(usage={**WIRE, 'prompt_tokens': 99, 'total_tokens': 119})], 'u', SCOPE)
    assert items[0]['fields']['prompt_tokens']['status'] == 'conflict'


def test_partial_subtotals_have_known_denominators_and_no_complete_total():
    items, _, _ = parse_deepagents([da(), da('two', usage=None)], 'u', SCOPE)
    result = summarize_observations(items)
    assert result['prompt_tokens']['known_partial_subtotal'] == 100
    assert result['prompt_tokens']['known_observations'] == 1
    assert result['prompt_tokens']['unknown_observations'] == 1
    assert result['cache_write_tokens']['known_partial_subtotal'] is None
    assert all(v['complete_research_total'] is False for v in result.values())


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = (json.dumps(value) + '\n').encode()
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def integration(tmp_path, candidate='deepagents_langgraph', *, rows=None):
    root = tmp_path / 'data'; root.mkdir(parents=True)
    run = root / f'runs/2026-10-21/{candidate}/tools/run-one'; run.mkdir(parents=True)
    manifest = {'candidate': candidate, 'run_id': 'run-one', 'candidate_exit_code': 0, 'grade_exit_code': 1}
    refs = []
    for name, value in [('manifest.json', manifest), ('verdict.json', {'passed': False})]:
        sha = write_json(run / name, value)
        refs.append({'path': (run / name).relative_to(root).as_posix(), 'sha256': sha})
    write_json(run / 'input.json', {'candidate': candidate, 'run_id': 'run-one', 'scope': SCOPE, 'prompt': 'NEVER_EMIT_SECRET'})
    entry = {'candidate': candidate, 'run_id': 'run-one', 'case': 'tools', 'budget_group': 'detailed',
        'run_path': run.relative_to(root).as_posix(), 'budget_path': 'budgets/detailed/run-one/run-one/calls.sqlite',
        'upstream_kind': 'remote', 'research_category': 'formal',
        'closure_kind': 'candidate-and-grade-terminal-exit-codes', 'closure_sources': refs}
    inventory = {'schema': 'harness-lab-research-usage-inventory/1', 'data_root': str(root), 'entries': [entry]}
    inv_path = tmp_path / 'inventory.json'; inv_sha = write_json(inv_path, inventory)
    proxy = {k: 0 for k in ('reservations', 'remote_forwarded', 'remote_forwarded_2xx', 'remote_forwarded_non2xx',
             'remote_attempt_unconfirmed', 'unknown_or_inflight', 'local_injected', 'recorded_rejected')}
    proxy.update(reservations=7, remote_forwarded=7, remote_forwarded_2xx=7)
    spend = {'schema': 'harness-lab-research-usage-summary/1', 'inventory': {'sha256': inv_sha},
        'unresolved_databases': [], 'deduplicated_aliases': [], 'closed_databases': [{**entry,
            'closure_facts': {'candidate_exit_code': 0, 'grade_exit_code': 1}, 'usage': proxy}]}
    spend_path = tmp_path / 'summary.json'; write_json(spend_path, spend)
    native = run / 'candidate/raw-events.jsonl'; native.parent.mkdir()
    native.write_text(''.join(json.dumps(row) + '\n' for row, _ in ([da()] if rows is None else rows)))
    return inv_path, spend_path, native


def test_end_to_end_failed_business_is_measurable_without_opening_database(tmp_path):
    inv, spend, native = integration(tmp_path)
    result = summarize(inv, spend)
    assert not (tmp_path / 'data/budgets').exists()
    assert result['enrolled_budget_entries'] == 1
    assert result['units'][0]['unique_usage_observations'] == 1
    assert result['units'][0]['proxy_reservations_by_outcome']['reservations'] == 7
    assert result['coverage_ratio'] is None and result['units'][0]['coverage_ratio'] is None
    assert 'NEVER_EMIT_SECRET' not in json.dumps(result) and 'NEVER_EMIT_REASONING' not in json.dumps(result)
    assert result['units'][0]['native_files'][0]['sha256'] == hashlib.sha256(native.read_bytes()).hexdigest()


@pytest.mark.parametrize('change', ['inventory_hash', 'unresolved', 'aliases', 'missing_closed_row', 'changed_entry'])
def test_summary_must_exactly_bind_the_frozen_inventory(tmp_path, change):
    inv, spend_path, _ = integration(tmp_path)
    spend = json.loads(spend_path.read_bytes())
    if change == 'inventory_hash': spend['inventory']['sha256'] = '0' * 64
    elif change == 'unresolved': spend['unresolved_databases'] = [{}]
    elif change == 'aliases': spend['deduplicated_aliases'] = [{}]
    elif change == 'missing_closed_row': spend['closed_databases'] = []
    else: spend['closed_databases'][0]['run_id'] = 'foreign'
    write_json(spend_path, spend)
    with pytest.raises(EvidenceError): summarize(inv, spend_path)


def test_changed_metadata_missing_native_file_and_partial_json_remain_unknown(tmp_path):
    inv, spend, native = integration(tmp_path)
    manifest = native.parent.parent / 'manifest.json'; old = manifest.read_bytes(); manifest.write_text('{}')
    result = summarize(inv, spend)
    assert result['units'][0]['issues'] == {'frozen_metadata_changed': 1}
    manifest.write_bytes(old); native.unlink()
    assert summarize(inv, spend)['units'][0]['status'] == 'unknown'
    native.write_text('{"content":"NEVER_EMIT_SECRET"')
    result = summarize(inv, spend)
    assert result['parse_error_units'] == 1 and result['units'][0]['observations'] == []
    assert result['units'][0]['native_files'][0]['sha256'] == hashlib.sha256(native.read_bytes()).hexdigest()
    assert 'NEVER_EMIT_SECRET' not in json.dumps(result)


def test_incomplete_frozen_terminal_sources_remain_unknown(tmp_path):
    inv_path, spend_path, _ = integration(tmp_path)
    inventory = json.loads(inv_path.read_bytes())
    inventory['entries'][0]['closure_sources'] = [source for source in inventory['entries'][0]['closure_sources']
                                                if not source['path'].endswith('/verdict.json')]
    inv_sha = write_json(inv_path, inventory)
    spend = json.loads(spend_path.read_bytes())
    spend['inventory']['sha256'] = inv_sha
    spend['closed_databases'][0]['closure_sources'] = inventory['entries'][0]['closure_sources']
    write_json(spend_path, spend)
    unit = summarize(inv_path, spend_path)['units'][0]
    assert unit['status'] == 'unknown' and unit['observations'] == []
    assert unit['issues'] == {'incomplete_frozen_closure_sources': 1}


def test_symlinks_old_paths_and_path_traversal_are_rejected(tmp_path):
    root = tmp_path / 'data'; root.mkdir()
    secret = tmp_path / 'secret.json'; secret.write_text('NEVER_EMIT_SECRET')
    (root / 'linked.json').symlink_to(secret)
    for path in ['../secret.json', '/absolute.json', 'old/input.json', 'Old/input.json', 'linked.json']:
        with pytest.raises(EvidenceError): within(root, path)
    inv, spend, native = integration(tmp_path / 'nested')
    native.unlink(); native.symlink_to(secret)
    result = summarize(inv, spend)
    assert result['units'][0]['status'] == 'unknown'
    assert 'NEVER_EMIT_SECRET' not in json.dumps(result)


def test_foreign_scope_rows_are_not_counted_or_echoed(tmp_path):
    inv, spend, _ = integration(tmp_path, rows=[da(scope={**SCOPE, 'user_id': 'other'})])
    result = summarize(inv, spend)
    assert result['units'][0]['observations'] == []
    assert result['units'][0]['issues'] == {'unbound_or_foreign_native_scope': 1}


def test_hardlinked_native_file_is_not_counted_as_a_distinct_original(tmp_path):
    inv, spend, native = integration(tmp_path)
    os.link(native, native.parent / 'second-native.jsonl')
    result = summarize(inv, spend)
    assert result['units'][0]['observations'] == []
    assert result['units'][0]['issues'] == {'hardlinked_evidence_file': 1}
    assert result['known_by_field']['total_tokens']['known_partial_subtotal'] is None


def scope_entry(tmp_path, *, kind=I24):
    trial = 'trial-one'; suffix = 'main-a'; task_id = 'task-a'; variant = trial + '-' + suffix
    case = 'scope-baseline' if kind == I24_BASE else 'scope'
    rel = f'runs/2026-10-21/deepagents_langgraph/{case}/{trial}'
    run = tmp_path / rel; run.mkdir(parents=True)
    experiment = 'multi-user-baseline' if kind == I24_BASE else 'multi-user'
    scope = {'experiment_id': experiment, 'variant': variant, 'user_id': 'user-a', 'session_id': 'session-a'}
    plan = {'trial_id': trial, 'candidate': 'deepagents_langgraph', 'baseline_only': kind == I24_BASE,
            'executed_tasks': [], 'tasks': []}
    registry = {'registrations': {}, 'retirements': {}}
    facts = {'budget_writers_closed': True, 'paid_route_cleanup_proven': True, 'writer_exits': {}}
    chains = []
    selected = [('task-a', 'main-a')] if kind == I24_BASE else [
        ('task-a', 'main-a'), ('task-b', 'main-b'), ('task-a-cancel', 'cancel-a'), ('task-b-cancel', 'cancel-b')]
    for selected_id, selected_suffix in selected:
        selected_variant = trial + '-' + selected_suffix
        selected_scope = {**scope, 'variant': selected_variant, 'user_id': 'user-' + selected_suffix[-1],
                          'session_id': 'session-' + selected_suffix[-1]}
        plan['executed_tasks'].append(selected_id)
        plan['tasks'].append({'task_id': selected_id, 'run': '/data/' + rel.rsplit('/', 1)[0] + '/' + selected_variant,
                              'scope': selected_scope})
        if kind == I24:
            chains.append((run.parent / selected_variant / 'chain.json', {'candidate': 'deepagents_langgraph',
                'chain_id': selected_variant, 'scope': selected_scope}))
        attempts = ('worker-a', 'worker-b') if kind == I24 and selected_suffix.startswith('main') else ('worker-a',)
        for attempt in attempts:
            name = 'harness-lab-' + trial + '-' + selected_suffix + '-' + attempt
            identity = hashlib.sha256(name.encode()).hexdigest()
            worker = {'id': identity, 'name': '/' + name, 'role': 'worker', 'trial_id': trial}
            registry['registrations'][identity] = worker
            registry['retirements'][identity] = {'after_stop': dict(worker), 'deleted': {'id': identity, 'id_absent': True}}
            facts['writer_exits'][name] = {'id': identity, 'task_id': selected_id, 'role': 'worker'}
    verdict, grade = (('baseline-verdict.json', 'bench-3-baseline-grade.container.json') if kind == I24_BASE else
                      ('scope-verdict.json', 'bench-7-grade.container.json'))
    sources = []
    metadata = [(run / 'plan.json', plan), (run / 'host-result.json', {}), (run / verdict, {}),
                (run / 'container-transition-registry.json', registry), (run / grade, {}), *chains]
    for path, value in metadata:
        sources.append({'path': path.relative_to(tmp_path).as_posix(), 'sha256': write_json(path, value)})
    entry = {'run_id': trial, 'candidate': 'deepagents_langgraph', 'case': case, 'run_path': rel,
             'closure_kind': kind, 'task_id': task_id, 'closure_sources': sources}
    return entry, run, scope, facts


def test_i24_only_the_bound_plan_sibling_is_used(tmp_path):
    entry, run, scope, facts = scope_entry(tmp_path)
    paths, resolved, refs = locate(tmp_path, entry, facts)
    assert resolved == scope and len(paths) == 2
    assert all(p.parents[3].name == 'trial-one-main-a' for p in paths)
    assert len(refs) == 9
    with pytest.raises(EvidenceError, match='unproven_closed_execution'):
        locate(tmp_path, entry, {'budget_writers_closed': True})


def test_i24_unfrozen_foreign_sibling_metadata_is_rejected(tmp_path):
    entry, _, _, facts = scope_entry(tmp_path)
    source = next(s for s in entry['closure_sources'] if '/trial-one-main-a/' in s['path'])
    source['path'] = source['path'].replace('trial-one-main-a', 'foreign')
    with pytest.raises(EvidenceError, match='unexpected_closure_source'):
        locate(tmp_path, entry, facts)


@pytest.mark.parametrize('missing', ['host-result.json', 'container-transition-registry.json',
                                    'trial-one-cancel-b/chain.json'])
def test_i24_incomplete_frozen_sources_are_not_treated_as_closed(tmp_path, missing):
    entry, _, _, facts = scope_entry(tmp_path)
    entry['closure_sources'] = [source for source in entry['closure_sources'] if not source['path'].endswith('/' + missing)]
    with pytest.raises(EvidenceError, match='incomplete_frozen_closure_sources'):
        locate(tmp_path, entry, facts)


@pytest.mark.parametrize('change', ['missing_registration', 'registration_id', 'retired_name', 'deleted_id', 'writer_id', 'writer_task'])
def test_i24_expected_attempt_requires_matching_frozen_lifecycle(tmp_path, change):
    entry, run, _, facts = scope_entry(tmp_path)
    registry_path = run / 'container-transition-registry.json'
    registry = json.loads(registry_path.read_bytes())
    name = 'harness-lab-trial-one-main-a-worker-b'
    identity = hashlib.sha256(name.encode()).hexdigest()
    if change == 'missing_registration': registry['registrations'].pop(identity)
    elif change == 'registration_id': registry['registrations'][identity]['id'] = 'f' * 64
    elif change == 'retired_name': registry['retirements'][identity]['after_stop']['name'] = '/foreign'
    elif change == 'deleted_id': registry['retirements'][identity]['deleted']['id'] = 'f' * 64
    elif change == 'writer_id': facts['writer_exits'][name]['id'] = 'f' * 64
    else: facts['writer_exits'][name]['task_id'] = 'task-b'
    sha = write_json(registry_path, registry)
    next(s for s in entry['closure_sources'] if s['path'].endswith('/container-transition-registry.json'))['sha256'] = sha
    with pytest.raises(EvidenceError, match='unbound_scope_worker_lifecycle'):
        locate(tmp_path, entry, facts)


def scope_integration(tmp_path, *, attempts=('worker-a',), kind=I24):
    root = tmp_path / 'data'
    entry, run, scope, facts = scope_entry(root, kind=kind)
    group = scope['experiment_id']; variant = scope['variant']
    entry.update(budget_group=group, budget_path=f'budgets/{group}/{variant}/{variant}/calls.sqlite',
                 upstream_kind='remote', research_category='formal')
    inventory = {'schema': 'harness-lab-research-usage-inventory/1', 'data_root': str(root), 'entries': [entry]}
    inv_path = tmp_path / 'inventory.json'; inv_sha = write_json(inv_path, inventory)
    proxy = {k: 0 for k in ('reservations', 'remote_forwarded', 'remote_forwarded_2xx', 'remote_forwarded_non2xx',
             'remote_attempt_unconfirmed', 'unknown_or_inflight', 'local_injected', 'recorded_rejected')}
    proxy.update(reservations=1, remote_forwarded=1, remote_forwarded_2xx=1)
    spend = {'schema': 'harness-lab-research-usage-summary/1', 'inventory': {'sha256': inv_sha},
        'unresolved_databases': [], 'deduplicated_aliases': [],
        'closed_databases': [{**entry, 'closure_facts': facts, 'usage': proxy}]}
    spend_path = tmp_path / 'summary.json'; write_json(spend_path, spend)
    for attempt in attempts:
        native = run.parent / 'trial-one-main-a' / 'attempts' / attempt / 'candidate/raw-events.jsonl'
        native.parent.mkdir(parents=True, exist_ok=True)
        native.write_text(json.dumps(da(scope=scope)[0]) + '\n')
    return inv_path, spend_path


@pytest.mark.parametrize('present,missing', [('worker-a', 'worker-b'), ('worker-b', 'worker-a')])
def test_i24_missing_expected_attempt_keeps_known_other_attempt_and_explicit_gap(tmp_path, present, missing):
    inv, spend = scope_integration(tmp_path, attempts=(present,))
    result = summarize(inv, spend); unit = result['units'][0]
    assert unit['status'] == 'projected_partial_with_evidence_gaps'
    assert unit['known_by_field']['total_tokens']['known_partial_subtotal'] == 120
    assert unit['unique_usage_observations'] == 1 and len(unit['native_files']) == 1
    assert unit['issues']['expected_native_evidence_missing'] == 1 and result['evidence_missing_units'] == 1
    assert len(unit['native_gaps']) == 1 and '/' + missing + '/' in unit['native_gaps'][0]['path']
    assert unit['native_gaps'][0]['reason'] == 'expected_native_evidence_missing'


def test_i24_both_expected_attempts_missing_remain_unknown(tmp_path):
    inv, spend = scope_integration(tmp_path, attempts=())
    result = summarize(inv, spend); unit = result['units'][0]
    assert unit['status'] == 'unknown' and len(unit['native_gaps']) == 2
    assert unit['observations'] == [] and unit['issues']['expected_native_evidence_missing'] == 2
    assert unit['known_by_field']['total_tokens']['known_partial_subtotal'] is None


def test_i24_baseline_uses_frozen_single_attempt_without_inventing_b_gap(tmp_path):
    inv, spend = scope_integration(tmp_path, kind=I24_BASE)
    unit = summarize(inv, spend)['units'][0]
    assert unit['status'] == 'projected_partial' and unit['native_gaps'] == []
    assert unit['known_by_field']['total_tokens']['known_partial_subtotal'] == 120


def test_i25_reads_only_frozen_task_owner_phase_and_ignores_prepared_history(tmp_path):
    trial = 'trial-one'; task_id = 'wake-user-001'
    rel = f'runs/2026-10-21/deepagents_langgraph/instance-cost/{trial}'; run = tmp_path / rel
    scope = {'experiment_id': 'instance-cost', 'variant': trial + '-' + task_id, 'user_id': 'user-001', 'session_id': task_id}
    task = {'task_id': task_id, 'owner': 'user-001', 'kind': 'wake', 'scope': scope}
    created, retired = 'protected/lifecycle-1.json', 'protected/retired-2.json'
    metadata = {'protected/plan.json': {'baseline_only': False}, 'protected/tasks.json': [task],
                'protected/flow-host.json': {}, 'protected/flow-verdict.json': {}, 'protected/flow-cleanup.json': {},
                'protected/final-containers.json': [], 'protected/grade-flow-container.json': {}, created: {}, retired: {}}
    sources = [{'path': (run / name).relative_to(tmp_path).as_posix(), 'sha256': write_json(run / name, value)}
               for name, value in metadata.items()]
    entry = {'run_id': trial, 'candidate': 'deepagents_langgraph', 'case': 'instance-cost', 'run_path': rel,
             'closure_kind': I25, 'task_id': task_id, 'closure_sources': sources,
             'proxy_created_source': created, 'proxy_retired_source': retired}
    facts = {'budget_writers_closed': True, 'cleanup_independently_proven': True}
    paths, actual, refs = locate(tmp_path, entry, facts)
    assert paths == [run / 'users/user-001/evidence/wake/raw-events.jsonl'] and actual == scope
    assert all('history' not in p.parts for p in paths) and len(refs) == 9
    entry['closure_sources'] = [s for s in sources if not s['path'].endswith('/protected/flow-cleanup.json')]
    with pytest.raises(EvidenceError, match='incomplete_frozen_closure_sources'):
        locate(tmp_path, entry, facts)


def test_empty_summary_never_turns_absence_into_measured_zero():
    assert all(item['known_partial_subtotal'] is None for item in summarize_observations([]).values())
