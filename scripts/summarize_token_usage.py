"""从明确登记且已闭合的原生证据提取 token 计数。

仅使用标准库。不打开数据库、导入候选、运行任务或打印原生载荷。闭合由独立核验的消耗汇总证明；本程序先核验清单绑定和冻结元数据，再定位受限支持的原生文件。缺失证据保持未知。
"""

import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import re
import stat


CANDIDATES = {'deepseek_harness', 'deepagents_langgraph', 'openclaw', 'openai_agents_sdk'}
FIELDS = ('prompt_tokens', 'completion_tokens', 'total_tokens', 'cache_read_tokens',
          'cache_write_tokens', 'reasoning_tokens')
KNOWN = {'reported', 'derived_from_reported_totals', 'derived_from_reported_components'}
MAX_COUNT = 2**53 - 1
MAX_FILE = 64 * 1024**2
IDENTIFIER = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z')
HEX = re.compile(r'[0-9a-f]{64}\Z')
I24 = 'finished-i24-scope-budget-writers-and-routes'
I24_BASE = 'finished-i24-baseline-budget-writers'
I25 = 'finished-instance-cost-flow-and-cleanup'
COMMON_METADATA = {'manifest.json', 'verdict.json', 'chain.json', 'orchestration.json', 'cleanup.json'}
SCOPE_METADATA = {'plan.json', 'host-result.json', 'scope-verdict.json', 'baseline-verdict.json',
                  'container-transition-registry.json', 'bench-7-grade.container.json',
                  'bench-3-baseline-grade.container.json'}
FLOW_METADATA = {'protected/plan.json', 'protected/tasks.json', 'protected/flow-host.json',
                 'protected/baseline-verdict.json', 'protected/flow-verdict.json',
                 'protected/flow-cleanup.json', 'protected/final-containers.json',
                 'protected/grade-flow-container.json'}


class EvidenceError(ValueError):
    """证据与输出边界仅允许通过固定原因码。"""


def require(condition, code):
    if not condition:
        raise EvidenceError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':')).encode()).hexdigest()


def identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None and value.lower() != 'old'


def absolute_path(value, *, exists=True):
    path = Path(value)
    require(path.is_absolute() and '..' not in path.parts and all(p.lower() != 'old' for p in path.parts),
            'unsafe_path')
    require(not any(p.is_symlink() for p in (path, *path.parents)), 'symlink_path')
    require(path.resolve(strict=exists) == path, 'noncanonical_path')
    return path


def within(root, relative):
    require(isinstance(relative, str), 'unsafe_relative_path')
    part = Path(relative)
    require(not part.is_absolute() and relative == part.as_posix() and relative not in ('', '.')
            and '..' not in part.parts and all(p.lower() != 'old' for p in part.parts), 'unsafe_relative_path')
    path = absolute_path(root / part, exists=False)
    require(path.is_relative_to(root), 'outside_data_root')
    return path


def read_bytes(path):
    path = absolute_path(path)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    try:
        before = os.fstat(descriptor)
        require(stat.S_ISREG(before.st_mode) and before.st_size <= MAX_FILE, 'unbounded_or_nonregular_file')
        require(before.st_nlink == 1, 'hardlinked_evidence_file')
        with os.fdopen(descriptor, 'rb', closefd=False) as stream:
            data = stream.read(MAX_FILE + 1)
        after = os.fstat(descriptor)
        require(len(data) <= MAX_FILE and (before.st_dev, before.st_ino, before.st_nlink, before.st_size, before.st_mtime_ns)
                == (after.st_dev, after.st_ino, after.st_nlink, after.st_size, after.st_mtime_ns), 'evidence_changed_while_reading')
        return data
    finally:
        os.close(descriptor)


def read_json(path):
    data = read_bytes(path)
    try:
        value = json.loads(data)
    except (UnicodeError, ValueError):
        raise EvidenceError('invalid_json') from None
    return value, hashlib.sha256(data).hexdigest()


def source_ref(root, path, sha, *, line=None, pointer=None):
    result = {'path': path.relative_to(root).as_posix(), 'sha256': sha}
    if line is not None:
        result['line'] = line
    if pointer is not None:
        result['json_pointer'] = pointer
    return result


def read_rows(root, path):
    data = read_bytes(path)
    sha = hashlib.sha256(data).hexdigest()
    # 被杀进程可能留下不完整末行。这是真实解析缺口，
    # 不修补或静默忽略，且不汇总该材料单元的总量。
    try:
        lines = data.decode('utf-8').splitlines()
        rows = [(json.loads(line), source_ref(root, path, sha, line=index))
                for index, line in enumerate(lines, 1) if line.strip()]
    except (UnicodeError, ValueError):
        error = EvidenceError('invalid_native_jsonl')
        error.source_reference = source_ref(root, path, sha)
        raise error from None
    require(all(isinstance(row, dict) for row, _ in rows), 'invalid_native_row')
    return rows, source_ref(root, path, sha)


def field(value=None, status='unknown'):
    return {'value': value, 'status': status}


def unknown_fields():
    return {key: field() for key in FIELDS}


def count(value):
    return type(value) is int and 0 <= value <= MAX_COUNT


def set_count(result, name, mapping, key):
    if key in mapping:
        value = mapping[key]
        result[name] = field(value, 'reported') if count(value) else field(status='conflict')


def nested(mapping, key):
    value = mapping.get(key)
    return value if isinstance(value, dict) else {}


def validate_arithmetic(result):
    def known(name):
        return result[name]['status'] in KNOWN
    def conflict(*names):
        for name in names:
            result[name] = field(status='conflict')
    if all(known(name) for name in ('prompt_tokens', 'completion_tokens', 'total_tokens')):
        if result['prompt_tokens']['value'] + result['completion_tokens']['value'] != result['total_tokens']['value']:
            conflict('prompt_tokens', 'completion_tokens', 'total_tokens')
    if known('reasoning_tokens') and known('completion_tokens'):
        if result['reasoning_tokens']['value'] > result['completion_tokens']['value']:
            conflict('reasoning_tokens', 'completion_tokens')
    if known('prompt_tokens'):
        available = [key for key in ('cache_read_tokens', 'cache_write_tokens') if known(key)]
        if sum(result[key]['value'] for key in available) > result['prompt_tokens']['value']:
            conflict('prompt_tokens', *available)
    return result


def normalize_usage(usage, kind):
    """按字段是否实际存在提取；不从 SDK 默认零推断缺失细分。"""
    result = unknown_fields()
    if not isinstance(usage, dict):
        return result
    if kind in ('dsh', 'oc'):
        names = ({'completion_tokens': 'outputTokens', 'total_tokens': 'totalTokens',
                  'cache_read_tokens': 'cacheReadTokens', 'cache_write_tokens': 'cacheWriteTokens',
                  'reasoning_tokens': 'reasoningTokens'} if kind == 'dsh' else
                 {'completion_tokens': 'output', 'total_tokens': 'totalTokens',
                  'cache_read_tokens': 'cacheRead', 'cache_write_tokens': 'cacheWrite',
                  'reasoning_tokens': 'reasoningTokens'})
        for name, key in names.items():
            set_count(result, name, usage, key)
        base_key = 'inputTokens' if kind == 'dsh' else 'input'
        base = usage.get(base_key)
        total, output = result['total_tokens'], result['completion_tokens']
        if total['status'] in KNOWN and output['status'] in KNOWN:
            value = total['value'] - output['value']
            if count(value):
                result['prompt_tokens'] = field(value, 'derived_from_reported_totals')
            else:
                result['prompt_tokens'] = field(status='conflict')
        elif count(base) and all(result[k]['status'] in KNOWN for k in ('cache_read_tokens', 'cache_write_tokens')):
            value = base + result['cache_read_tokens']['value'] + result['cache_write_tokens']['value']
            result['prompt_tokens'] = field(value, 'derived_from_reported_components') if count(value) else field(status='conflict')
        if base_key in usage and not count(base):
            result['prompt_tokens'] = field(status='conflict')
        if count(base) and result['prompt_tokens']['status'] in KNOWN:
            keys = ('cache_read_tokens', 'cache_write_tokens')
            lower = base + sum(result[k]['value'] for k in keys if result[k]['status'] in KNOWN)
            complete = all(result[k]['status'] in KNOWN for k in keys)
            prompt = result['prompt_tokens']['value']
            if lower > prompt or (complete and lower != prompt):
                result['prompt_tokens'] = field(status='conflict')
    else:
        wire = kind == 'wire'
        for name, key in [('prompt_tokens', 'prompt_tokens' if wire else 'input_tokens'),
                          ('completion_tokens', 'completion_tokens' if wire else 'output_tokens'),
                          ('total_tokens', 'total_tokens')]:
            set_count(result, name, usage, key)
        prompt_details = nested(usage, 'prompt_tokens_details' if wire else 'input_token_details')
        output_details = nested(usage, 'completion_tokens_details' if wire else 'output_token_details')
        set_count(result, 'cache_read_tokens', prompt_details, 'cached_tokens' if wire else 'cache_read')
        set_count(result, 'cache_write_tokens', prompt_details, 'cache_write_tokens' if wire else 'cache_creation')
        set_count(result, 'reasoning_tokens', output_details, 'reasoning_tokens' if wire else 'reasoning')
        # 仅在标准细分缺失时，才使用 DeepSeek 旧版显式缓存命中字段；
        # 两种来源相互矛盾则记为冲突。
        if wire and 'prompt_cache_hit_tokens' in usage:
            alternate = usage['prompt_cache_hit_tokens']
            existing = result['cache_read_tokens']
            if not count(alternate) or (existing['status'] in KNOWN and alternate != existing['value']):
                result['cache_read_tokens'] = field(status='conflict')
            elif existing['status'] == 'unknown':
                result['cache_read_tokens'] = field(alternate, 'reported')
    return validate_arithmetic(result)


def merge_fields(left, right):
    result = {}
    for name in FIELDS:
        a, b = left[name], right[name]
        if a['status'] == 'conflict' or b['status'] == 'conflict':
            result[name] = field(status='conflict')
        elif a['status'] == 'unknown':
            result[name] = dict(b)
        elif b['status'] == 'unknown' or a['value'] == b['value']:
            result[name] = dict(a)
        else:
            result[name] = field(status='conflict')
    return validate_arithmetic(result)


def observation(identity, fields, refs, kind):
    return {'identity_sha256': digest(identity), 'kind': kind, 'fields': fields, 'sources': refs}


def add_observation(items, identity, fields, ref, kind):
    key = digest(identity)
    if key not in items:
        items[key] = observation(identity, fields, [], kind)
    else:
        items[key]['fields'] = merge_fields(items[key]['fields'], fields)
    if ref not in items[key]['sources']:
        items[key]['sources'].append(ref)


def scope_equal(actual, expected):
    return isinstance(actual, dict) and actual == expected


def parse_dsh(rows, unit, scope):
    events = {}; observed = 0
    for row, ref in rows:
        require(row.get('method') == 'session.event' and isinstance(row.get('params'), dict), 'unsupported_dsh_envelope')
        params = row['params']
        require(params.get('sessionId') == scope['session_id'], 'foreign_native_scope')
        event = params.get('event')
        require(isinstance(event, dict) and count(event.get('seq')) and isinstance(event.get('type'), str), 'invalid_dsh_event')
        seq = event['seq']
        data = event.get('data', {})
        if isinstance(data, dict):
            usage = nested(data, 'chunk').get('usage') if event['type'] == 'assistant/chunk' else data.get('usage')
            observed += event['type'] in ('assistant/chunk', 'assistant/message', 'compaction/summary') and isinstance(usage, dict)
        if seq in events:
            require(digest(event) == digest(events[seq][0]), 'native_event_identity_conflict')
            events[seq][1].append(ref)
        else:
            events[seq] = (event, [ref])
    attempts = Counter(); items = {}; issues = Counter()
    for seq, (event, refs) in sorted(events.items()):
        kind, data = event['type'], event.get('data')
        if not isinstance(data, dict):
            continue
        if kind == 'compaction/summary' and 'usage' in data:
            valid = data.get('llmStreamCall') is True
            fields = normalize_usage(data['usage'], 'dsh') if valid else unknown_fields()
            if not valid:
                issues['unproven_compaction_call'] += 1
            for ref in refs:
                add_observation(items, [unit, 'compaction', seq], fields, ref, 'auxiliary_response')
            continue
        is_chunk = kind == 'assistant/chunk' and isinstance(data.get('chunk'), dict) and data['chunk'].get('type') == 'usage'
        if kind not in ('llm/retry-started', 'assistant/message') and not is_chunk:
            continue
        require(count(data.get('turn')) and count(data.get('step')), 'invalid_dsh_attempt_boundary')
        pair = (data['turn'], data['step'])
        if kind == 'llm/retry-started':
            attempts[pair] += 1
            continue
        usage = data['chunk'].get('usage') if is_chunk else data.get('usage')
        if usage is None:
            issues['response_without_usage'] += 1
        else:
            require(isinstance(usage, dict), 'invalid_usage_object')
        identity = [unit, 'model', *pair, attempts[pair]]
        # 同一 attempt 的后续样本替换前样本，与原生折叠规则一致；
        # 但不能借此抹去之前无效或矛盾的样本。
        key = digest(identity); fields = normalize_usage(usage, 'dsh')
        if key in items:
            prior = items[key]
            for name in FIELDS:
                if prior['fields'][name]['status'] == 'conflict':
                    fields[name] = field(status='conflict')
                elif fields[name]['status'] == 'unknown':
                    fields[name] = dict(prior['fields'][name])
            prior['fields'] = validate_arithmetic(fields)
            prior['sources'].extend(ref for ref in refs if ref not in prior['sources'])
        else:
            items[key] = observation(identity, fields, list(refs), 'model_response')
    return list(items.values()), observed, issues


def parse_deepagents(rows, unit, scope):
    items = {}; observed = 0; issues = Counter()
    for row, ref in rows:
        if row.get('stream_mode') != 'updates':
            continue
        data = row.get('data')
        if not isinstance(data, dict) or not isinstance(data.get('model'), dict):
            continue
        # 仅读取模型节点输出的增量。不搜索 checkpoint 值、
        # 任务输入或结果，以及 resident 准备的历史。
        require(scope_equal(row.get('scope'), scope), 'unbound_or_foreign_native_scope')
        messages = data['model'].get('messages')
        require(isinstance(messages, list), 'unsupported_model_update')
        for index, message in enumerate(messages):
            require(isinstance(message, dict) and message.get('type') == 'ai', 'unsupported_model_message')
            native_id = message.get('id')
            require(isinstance(native_id, str) and 0 < len(native_id) <= 1024, 'missing_native_response_identity')
            usage = message.get('usage_metadata')
            require(usage is None or isinstance(usage, dict), 'invalid_usage_object')
            observed += usage is not None
            if usage is None:
                issues['response_without_usage'] += 1
            pointer = dict(ref, json_pointer=f'/data/model/messages/{index}/usage_metadata')
            add_observation(items, [unit, scope['session_id'], native_id], normalize_usage(usage, 'da'), pointer, 'model_update_response')
    return list(items.values()), observed, issues


def parse_openclaw(rows, unit, scope):
    native_session = 'agent:lab-a:openai-user:lab-' + hashlib.sha256(json.dumps(
        [scope['variant'], scope['session_id']], ensure_ascii=False, separators=(',', ':')).encode()).hexdigest()
    items = {}; observed = 0; issues = Counter()
    for row, ref in rows:
        if row.get('type') != 'agent_end':
            continue
        event, context = row.get('event'), row.get('context')
        require(isinstance(event, dict) and isinstance(context, dict)
                and context.get('agentId') == 'lab-a' and context.get('sessionKey') == native_session,
                'unbound_or_foreign_native_scope')
        require(isinstance(event.get('runId'), str) and event['runId'] == context.get('runId'), 'unbound_native_agent_run')
        messages = event.get('messages')
        require(isinstance(messages, list), 'unsupported_agent_end')
        for index, message in enumerate(messages):
            if not isinstance(message, dict) or message.get('role') != 'assistant':
                continue
            usage = message.get('usage'); response = message.get('responseId')
            ref = dict(ref, json_pointer=f'/event/messages/{index}/usage')
            measured = isinstance(usage, dict) and nested(usage, 'usageReport').get('state') != 'unavailable'
            explicitly_available = nested(usage, 'usageReport').get('state') == 'available' if isinstance(usage, dict) else False
            nonzero = any(count(usage.get(key)) and usage[key] > 0 for key in
                          ('input', 'output', 'cacheRead', 'cacheWrite', 'totalTokens')) if isinstance(usage, dict) else False
            identified = isinstance(response, str) and 0 < len(response) <= 1024
            identity = ([unit, native_session, response] if identified else
                        [unit, 'unmeasured', ref['path'], ref['line'], index])
            if not (measured and identified and (explicitly_available or nonzero)):
                issues['unmeasured_or_unidentified_assistant'] += 1
                add_observation(items, identity, unknown_fields(), ref, 'unmeasured_assistant')
                continue
            observed += 1
            add_observation(items, identity, normalize_usage(usage, 'oc'), ref, 'model_response')
            items[digest(identity)]['kind'] = 'model_response'
    return list(items.values()), observed, issues


def parse_sdk(rows, unit, scope):
    attempts = {}; observed = 0; issues = Counter()
    for row, ref in rows:
        if row.get('type') not in ('provider_http_response', 'provider_wire_event'):
            continue
        require(scope_equal(row.get('scope'), scope), 'unbound_or_foreign_native_scope')
        event = row.get('event')
        require(isinstance(event, dict) and count(event.get('attempt')) and event['attempt'] > 0, 'invalid_transport_attempt')
        key = (ref['path'], event['attempt'])
        if row['type'] == 'provider_http_response':
            require(key not in attempts and type(event.get('status')) is int and type(event.get('sse')) is bool,
                    'invalid_transport_response')
            attempts[key] = {'status': event['status'], 'sse': event['sse'], 'refs': [ref], 'ids': set(), 'usage': []}
        else:
            require(key in attempts, 'wire_without_transport_response')
            data = event.get('data')
            require(isinstance(data, dict), 'invalid_wire_event')
            native_id = data.get('id')
            if isinstance(native_id, str) and 0 < len(native_id) <= 1024:
                attempts[key]['ids'].add(native_id)
            if data.get('usage') is not None:
                require(isinstance(data['usage'], dict), 'invalid_usage_object')
                observed += 1
                attempts[key]['usage'].append((data['usage'], dict(ref, json_pointer='/event/data/usage')))
    items = {}
    for (path, attempt), response in attempts.items():
        if not (200 <= response['status'] < 300):
            issues['transport_without_supported_successful_sse'] += 1
            continue
        if not response['sse']:
            issues['transport_without_supported_successful_sse'] += 1
            add_observation(items, [unit, path, attempt, 'non_sse'], unknown_fields(),
                            response['refs'][0], 'transport_response_unknown')
            continue
        ids = response['ids']
        if len(ids) != 1:
            issues['missing_or_conflicting_wire_response_identity'] += 1
            add_observation(items, [unit, path, attempt, 'unidentified'], unknown_fields(), response['refs'][0], 'transport_response_unknown')
            continue
        identity = [unit, path, attempt, next(iter(ids))]
        if not response['usage']:
            issues['response_without_usage'] += 1
            add_observation(items, identity, unknown_fields(), response['refs'][0], 'transport_response_unknown')
        for usage, ref in response['usage']:
            add_observation(items, identity, normalize_usage(usage, 'wire'), ref, 'provider_wire_response')
    return list(items.values()), observed, issues


PARSERS = {'deepseek_harness': parse_dsh, 'deepagents_langgraph': parse_deepagents,
           'openclaw': parse_openclaw, 'openai_agents_sdk': parse_sdk}


def frozen_metadata(root, entry):
    run = within(root, entry['run_path'])
    require(run.name == entry['run_id'] and run.parent.name == entry['case']
            and run.parent.parent.name == entry['candidate'], 'invalid_run_identity')
    result = {}; refs = []
    for source in entry['closure_sources']:
        path = within(root, source['path'])
        if path.is_relative_to(run):
            name = path.relative_to(run).as_posix()
            allowed = name in COMMON_METADATA
            if entry['closure_kind'] in (I24, I24_BASE):
                allowed = name in SCOPE_METADATA
            elif entry['closure_kind'] == I25:
                allowed = name in FLOW_METADATA or (name in (entry.get('proxy_created_source'), entry.get('proxy_retired_source'))
                          and re.fullmatch(r'protected/(lifecycle|retired)-[1-9][0-9]*\.json', name))
        else:
            name = ''
            if entry['closure_kind'] == I24:
                for suffix in ('main-a', 'main-b', 'cancel-a', 'cancel-b'):
                    if path == run.parent / (run.name + '-' + suffix) / 'chain.json':
                        name = 'task-chain-' + suffix
            allowed = bool(name)
        require(allowed and name not in result, 'unexpected_closure_source')
        document, sha = read_json(path)
        require(sha == source['sha256'], 'frozen_metadata_changed')
        result[name] = document
        refs.append(source_ref(root, path, sha))
    kind = entry['closure_kind']
    if kind == I24:
        required = (SCOPE_METADATA - {'baseline-verdict.json', 'bench-3-baseline-grade.container.json'}) | {
            'task-chain-' + suffix for suffix in ('main-a', 'main-b', 'cancel-a', 'cancel-b')}
    elif kind == I24_BASE:
        required = SCOPE_METADATA - {'scope-verdict.json', 'bench-7-grade.container.json'}
    elif kind == I25:
        plan = result.get('protected/plan.json')
        require(isinstance(plan, dict) and type(plan.get('baseline_only')) is bool, 'unbound_flow_plan')
        created, retired = entry.get('proxy_created_source'), entry.get('proxy_retired_source')
        require(isinstance(created, str) and re.fullmatch(r'protected/lifecycle-[1-9][0-9]*\.json', created)
                and isinstance(retired, str) and re.fullmatch(r'protected/retired-[1-9][0-9]*\.json', retired),
                'unexpected_proxy_lifecycle_source')
        required = FLOW_METADATA - {'protected/baseline-verdict.json', 'protected/flow-verdict.json'}
        required |= {created, retired, 'protected/baseline-verdict.json' if plan['baseline_only'] else 'protected/flow-verdict.json'}
    elif kind == 'candidate-and-grade-terminal-exit-codes':
        required = {'manifest.json', 'verdict.json'}
    elif kind == 'finished-orchestration-and-cleanup':
        required = {'orchestration.json', 'cleanup.json'}
    elif kind == 'finished-fault-chain-and-cleanup':
        required = {'chain.json', 'orchestration.json', 'cleanup.json'}
    else:
        raise EvidenceError('unsupported_closure_structure')
    require(required <= result.keys(), 'incomplete_frozen_closure_sources')
    return run, result, refs


def bound_scope(value, variant, *, owner=None, session=None):
    require(isinstance(value, dict) and set(value) == {'experiment_id', 'variant', 'user_id', 'session_id'}
            and all(identifier(v) for v in value.values()) and value['variant'] == variant
            and (owner is None or value['user_id'] == owner)
            and (session is None or value['session_id'] == session), 'invalid_trusted_scope')
    return value


def i24_attempts(documents, facts, run, task_id, suffix, kind):
    """预期文件依据已闭合的登记执行确定，不按文件是否存在猜测。"""
    registry = documents.get('container-transition-registry.json')
    require(isinstance(registry, dict) and isinstance(registry.get('registrations'), dict)
            and isinstance(registry.get('retirements'), dict), 'unbound_scope_worker_lifecycle')
    attempts = ('worker-a', 'worker-b') if suffix.startswith('main') and kind == I24 else ('worker-a',)
    for attempt in attempts:
        name = 'harness-lab-' + run.name + '-' + suffix + '-' + attempt
        matches = [(identity, row) for identity, row in registry['registrations'].items()
                   if isinstance(row, dict) and row.get('name') == '/' + name]
        require(len(matches) == 1, 'unbound_scope_worker_lifecycle')
        identity, registered = matches[0]
        require(isinstance(identity, str) and HEX.fullmatch(identity) is not None
                and registered.get('id') == identity and registered.get('role') == 'worker'
                and registered.get('trial_id') == run.name, 'unbound_scope_worker_lifecycle')
        retirement = registry['retirements'].get(identity)
        stopped = retirement.get('after_stop') if isinstance(retirement, dict) else None
        deleted = retirement.get('deleted') if isinstance(retirement, dict) else None
        require(isinstance(stopped, dict) and stopped.get('id') == identity
                and stopped.get('name') == '/' + name and stopped.get('role') == 'worker'
                and isinstance(deleted, dict) and deleted.get('id') == identity and deleted.get('id_absent') is True,
                'unbound_scope_worker_lifecycle')
        if kind == I24:
            writers = facts.get('writer_exits')
            writer = writers.get(name) if isinstance(writers, dict) else None
            require(isinstance(writer, dict) and writer.get('id') == identity
                    and writer.get('task_id') == task_id and writer.get('role') == 'worker',
                    'unbound_scope_worker_lifecycle')
    return attempts


def locate(root, entry, facts):
    run, documents, refs = frozen_metadata(root, entry)
    kind = entry['closure_kind']
    if kind == I25:
        require(facts.get('budget_writers_closed') is True and facts.get('cleanup_independently_proven') is True,
                'unproven_closed_execution')
        tasks = documents.get('protected/tasks.json')
        require(isinstance(tasks, list), 'unsupported_flow_tasks')
        matches = [t for t in tasks if isinstance(t, dict) and t.get('task_id') == entry.get('task_id')]
        require(len(matches) == 1, 'unbound_flow_task')
        task = matches[0]; owner, phase = task.get('owner'), task.get('kind')
        require(identifier(owner) and phase in ('baseline', 'active', 'wake', 'fault')
                and task['task_id'] == phase + '-' + owner, 'unbound_flow_task')
        scope = bound_scope(task.get('scope'), run.name + '-' + task['task_id'], owner=owner, session=task['task_id'])
        return [run / 'users' / owner / 'evidence' / phase / 'raw-events.jsonl'], scope, refs
    if kind in (I24, I24_BASE):
        require(facts.get('budget_writers_closed') is True and (kind != I24 or facts.get('paid_route_cleanup_proven') is True),
                'unproven_closed_execution')
        plan = documents.get('plan.json')
        require(isinstance(plan, dict) and plan.get('trial_id') == run.name
                and plan.get('candidate') == entry['candidate'], 'unbound_scope_plan')
        tasks = plan.get('tasks')
        require(isinstance(tasks, list), 'unbound_scope_task')
        matches = [t for t in tasks if isinstance(t, dict) and t.get('task_id') == entry.get('task_id')]
        require(len(matches) == 1 and entry['task_id'] in plan.get('executed_tasks', []), 'unbound_scope_task')
        task = matches[0]
        suffixes = {'task-a': 'main-a', 'task-b': 'main-b', 'task-a-cancel': 'cancel-a', 'task-b-cancel': 'cancel-b'}
        require(entry['task_id'] in suffixes, 'unsupported_scope_task')
        suffix = suffixes[entry['task_id']]; variant = run.name + '-' + suffix
        target = run.parent / variant
        require(task.get('run') == '/data/' + target.relative_to(root).as_posix(), 'foreign_scope_run_path')
        scope = bound_scope(task.get('scope'), variant, owner='user-' + suffix[-1], session='session-' + suffix[-1])
        if kind == I24:
            chain = documents.get('task-chain-' + suffix)
            require(isinstance(chain, dict) and chain.get('candidate') == entry['candidate']
                    and chain.get('chain_id') == variant and chain.get('scope') == scope, 'unbound_scope_chain')
        attempts = i24_attempts(documents, facts, run, entry['task_id'], suffix, kind)
        return [target / 'attempts' / attempt / 'candidate/raw-events.jsonl' for attempt in attempts], scope, refs
    if kind == 'candidate-and-grade-terminal-exit-codes':
        manifest = documents.get('manifest.json')
        require(isinstance(manifest, dict) and manifest.get('run_id') == run.name
                and manifest.get('candidate') == entry['candidate']
                and type(manifest.get('candidate_exit_code')) is int and type(manifest.get('grade_exit_code')) is int
                and facts.get('candidate_exit_code') == manifest['candidate_exit_code']
                and facts.get('grade_exit_code') == manifest['grade_exit_code'], 'unproven_closed_execution')
        input_path = run / 'input.json'
        task, sha = read_json(input_path)
        require(isinstance(task, dict) and task.get('run_id') == run.name and task.get('candidate') == entry['candidate'],
                'unbound_task_input')
        scope = bound_scope(task.get('scope'), run.name, owner='alice', session='main')
        refs.append(source_ref(root, input_path, sha))
        return [run / 'candidate/raw-events.jsonl'], scope, refs
    if kind in ('finished-orchestration-and-cleanup', 'finished-fault-chain-and-cleanup'):
        orch, cleanup = documents.get('orchestration.json'), documents.get('cleanup.json')
        require(isinstance(orch, dict) and isinstance(cleanup, dict) and orch.get('chain_id') == run.name
                and isinstance(orch.get('finished_at'), str) and bool(orch['finished_at'])
                and isinstance(cleanup.get('finished_at'), str) and bool(cleanup['finished_at'])
                and cleanup.get('remaining') == [] and facts.get('cleanup_independently_proven') is True,
                'unproven_closed_execution')
        chain = documents.get('chain.json')
        if chain is None:
            chain, sha = read_json(run / 'chain.json')
            refs.append(source_ref(root, run / 'chain.json', sha))
        require(isinstance(chain, dict) and chain.get('chain_id') == run.name
                and chain.get('candidate') == entry['candidate'], 'unbound_native_chain')
        scope = bound_scope(chain.get('scope'), run.name, owner='alice', session='main')
        paths = [run / 'attempts' / attempt / 'candidate/raw-events.jsonl' for attempt in ('worker-a', 'worker-b')]
        # B 可能从未启动；不遍历目录。
        return [p for p in paths if p.exists()], scope, refs
    raise EvidenceError('unsupported_closure_structure')


def summarize_observations(observations):
    result = {}
    for name in FIELDS:
        counts = Counter(o['fields'][name]['status'] for o in observations)
        known_count = sum(counts[k] for k in KNOWN)
        subtotal = sum(o['fields'][name]['value'] for o in observations if o['fields'][name]['status'] in KNOWN)
        result[name] = {'known_observations': known_count,
                        'unknown_observations': counts['unknown'], 'conflict_observations': counts['conflict'],
                        'known_partial_subtotal': subtotal if known_count and count(subtotal) else None,
                        'subtotal_overflow': not count(subtotal), 'complete_research_total': False}
    return result


def summarize(inventory_path, spend_summary_path):
    inventory, inventory_sha = read_json(absolute_path(inventory_path))
    spend, spend_sha = read_json(absolute_path(spend_summary_path))
    require(isinstance(inventory, dict) and inventory.get('schema') == 'harness-lab-research-usage-inventory/1'
            and isinstance(inventory.get('entries'), list), 'invalid_inventory')
    require(isinstance(spend, dict) and spend.get('schema') == 'harness-lab-research-usage-summary/1'
            and nested(spend, 'inventory').get('sha256') == inventory_sha, 'spend_inventory_hash_mismatch')
    require(spend.get('unresolved_databases') == [] and spend.get('deduplicated_aliases') == [],
            'spend_summary_not_fully_validated')
    root = absolute_path(inventory['data_root'])
    rows = spend.get('closed_databases')
    require(isinstance(rows, list) and all(isinstance(r, dict) for r in rows), 'invalid_closed_database_summary')
    closed = {r.get('budget_path'): r for r in rows}
    require(len(closed) == len(rows), 'duplicate_closed_database_summary')
    entries = inventory['entries']
    require(all(isinstance(e, dict) for e in entries)
            and len({e.get('budget_path') for e in entries}) == len(entries), 'duplicate_inventory_entry')
    require(set(closed) == {e.get('budget_path') for e in entries}, 'closed_inventory_set_mismatch')
    units = []; all_observations = []; excluded = Counter(); candidate_totals = {}; seen_units = set()
    for entry in entries:
        frozen = closed[entry['budget_path']]
        require(all(frozen.get(k) == value for k, value in entry.items()), 'closed_inventory_entry_mismatch')
        candidate = entry.get('candidate')
        if candidate not in CANDIDATES or entry.get('upstream_kind') != 'remote':
            excluded['outside_four_candidate_remote_scope'] += 1
            continue
        require(identifier(entry.get('run_id')) and isinstance(entry.get('closure_sources'), list), 'invalid_entry_identity')
        # 相对路径仅在核验后输出；不打开其指向文件。
        within(root, entry['budget_path'])
        facts = frozen.get('closure_facts')
        require(isinstance(facts, dict), 'missing_closed_execution_facts')
        usage = frozen.get('usage')
        require(isinstance(usage, dict), 'missing_proxy_classification')
        proxy_keys = ('reservations', 'remote_forwarded', 'remote_forwarded_2xx', 'remote_forwarded_non2xx',
                      'remote_attempt_unconfirmed', 'unknown_or_inflight', 'local_injected', 'recorded_rejected')
        require(all(count(usage.get(k)) for k in proxy_keys), 'invalid_proxy_classification')
        unit_id = digest([candidate, entry['run_path'], entry.get('task_id')])
        require(unit_id not in seen_units, 'duplicate_native_evidence_unit')
        seen_units.add(unit_id)
        result = {'unit_sha256': unit_id, 'candidate': candidate, 'budget_path': entry['budget_path'],
                  'proxy_reservations_by_outcome': {k: usage[k] for k in proxy_keys},
                  'request_linkage': 'not_established', 'coverage_ratio': None,
                  'status': 'unknown', 'issues': {}, 'metadata_sources': [], 'native_files': [],
                  'native_gaps': [], 'usage_observations_before_dedup': 0, 'observations': []}
        try:
            paths, scope, refs = locate(root, entry, facts)
            result['metadata_sources'] = refs
            require(paths, 'native_evidence_missing')
            native_rows = []
            for path in paths:
                checked = within(root, path.relative_to(root).as_posix())
                if entry['closure_kind'] in (I24, I24_BASE) and not checked.exists():
                    result['native_gaps'].append({'path': checked.relative_to(root).as_posix(),
                                                 'reason': 'expected_native_evidence_missing'})
                    continue
                require(checked.is_file(), 'native_evidence_missing')
                parsed, reference = read_rows(root, checked)
                result['native_files'].append(reference)
                native_rows.extend(parsed)
            observations, before, issues = PARSERS[candidate](native_rows, unit_id, scope)
            result.update(status='projected_partial', observations=observations,
                          usage_observations_before_dedup=before, issues=dict(issues))
            if not observations:
                result['issues']['no_supported_response_observations'] = 1
            if not any(value['status'] in KNOWN for item in observations for value in item['fields'].values()):
                result['status'] = 'unknown'
                result['issues']['no_measured_token_fields'] = 1
        except (EvidenceError, OSError, KeyError, TypeError) as error:
            reason = str(error) if isinstance(error, EvidenceError) else 'metadata_or_evidence_unavailable'
            result['issues'] = {reason: 1}
            if isinstance(error, EvidenceError) and hasattr(error, 'source_reference'):
                result['native_files'].append(error.source_reference)
        if result['native_gaps']:
            result['issues']['expected_native_evidence_missing'] = len(result['native_gaps'])
            if result['status'] == 'projected_partial':
                result['status'] = 'projected_partial_with_evidence_gaps'
        result['unique_usage_observations'] = len(result['observations'])
        result['unmeasured_remote_activity'] = ('possible_unmeasured_activity_no_request_join'
                                               if usage['remote_forwarded'] or usage['remote_attempt_unconfirmed']
                                               or usage['unknown_or_inflight'] else 'no_recorded_remote_forward')
        result['known_by_field'] = summarize_observations(result['observations'])
        units.append(result); all_observations.extend(result['observations'])
    for candidate in sorted(CANDIDATES):
        selected = [u for u in units if u['candidate'] == candidate]
        observations = [o for u in selected for o in u['observations']]
        candidate_totals[candidate] = {'enrolled_budget_entries': len(selected),
            'unknown_units': sum(u['status'] == 'unknown' for u in selected),
            'usage_observations_before_dedup': sum(u['usage_observations_before_dedup'] for u in selected),
            'unique_usage_observations': len(observations), 'known_by_field': summarize_observations(observations),
            'coverage_ratio': None, 'request_linkage': 'not_established'}
    return {'schema': 'harness-lab-token-usage-summary/1',
        'inventory': {'sha256': inventory_sha}, 'spend_summary': {'sha256': spend_sha},
        'claim': '仅包含已知的部分供应商或原生计数；不据此推断完整账单、请求覆盖率或金额',
        'enrolled_budget_entries': len(units),
        'unique_run_directories': len({e['run_path'] for e in entries if e.get('candidate') in CANDIDATES and e.get('upstream_kind') == 'remote'}),
        'excluded_entries': dict(excluded), 'units': units, 'by_candidate': candidate_totals,
        'units_without_usage': sum(not u['usage_observations_before_dedup'] for u in units),
        'evidence_missing_units': sum('native_evidence_missing' in u['issues'] or 'expected_native_evidence_missing' in u['issues']
                                      or 'metadata_or_evidence_unavailable' in u['issues'] for u in units),
        'parse_error_units': sum('invalid_native_jsonl' in u['issues'] or 'invalid_json' in u['issues'] for u in units),
        'known_by_field': summarize_observations(all_observations),
        'request_linkage': 'not_established', 'coverage_ratio': None,
        'limitations': ['不执行 SQL 或模型；消耗请求分类直接复制已核验汇总。',
            '仅读取受限支持的原生主来源；不输出 checkpoint、原始推理、响应正文或费用字段。',
            'Deep Agents 模型 updates 排除重放输入和 checkpoint 历史；本版不恢复缺少模型 update 的纯 usage 流片段。',
            '不使用 SDK 原生默认零 Usage 或合成响应 ID；仅支持绑定 scope 的传输层原始事件。',
            'I24 预期 attempt 文件按冻结且已登记或回收的 Worker 确定；缺失文件是明确证据缺口，不能证明执行从未开始。',
            'I25 仅支持冻结清单中的 baseline/active/wake/fault 任务；不支持 inactive-wake 和后续轮换任务。',
            '无 scope 的旧格式、缺失或变化的证据以及未支持结构均保持未知。',
            '已知小计不是完整调研总量；缺失的缓存或推理字段保持 null。']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--spend-summary', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        output = absolute_path(args.output, exists=False)
        require(output.suffix == '.json' and not output.exists() and output.parent.is_dir(), 'output_requires_new_json_file')
        result = summarize(args.inventory, args.spend_summary)
        # 独占输出不替换旧证据。即使格式错误的输入包含密钥文本，
        # 也不打印来源载荷或异常。
        with output.open('x') as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write('\n')
        print(json.dumps({'status': 'written', 'enrolled_budget_entries': result['enrolled_budget_entries'],
                          'unknown_units': sum(u['status'] == 'unknown' for u in result['units'])}))
    except (EvidenceError, OSError, KeyError, TypeError):
        print(json.dumps({'status': 'refused', 'reason': 'token_evidence_or_input_validation_failed'}))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
