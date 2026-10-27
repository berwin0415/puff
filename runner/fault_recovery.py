"""I23 实现等待 Docker 校准，尚不声称具备恢复能力。

独立准备、控制和五维判定。不运行 Agent，不重试业务，
也不根据 I22 暂停快照推断崩溃恢复。Deep Agents 使用原始 SQLite 头检查点；
DSH 先执行官方中断轮次修复，再使用同一原始 Goal。两个入口均待 Docker 校准。"""
from __future__ import annotations

import argparse
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import tempfile
import time

import httpx

from runner.bench import environment, identifier
from runner.config import load_config
from runner import detailed
from runner import pause_recovery as io
from runner.fault_outcome import controlled_failure
from runner.dsh_goal import fold_durable_goal, matches_live_view
from runner.experiment import ledger_request
from runner.grading import audited_calls, expected_tools, grade_tools, prepare_tools_inputs, same_scope, scoped_ledger

CANDIDATES = ('deepagents_langgraph', 'deepseek_harness')
FAULTS = ('none', 'model_timeout', 'fail_once', 'drop_receipt_once', 'kill_before_write')
ACTIONS = ('prepare', 'configure', 'bind-start', 'observe-gate', 'after-kill', 'bind-crash-resume', 'grade')
# 此处只可登记已审查且随镜像打包的 Python 函数。用户
# JSON 文件、候选结果或 I22 暂停索引均不能激活这些路径。
NATIVE_START_BINDERS = {}
NATIVE_CRASH_BINDERS = {}
require, read, create, regular, sha, digest = io.require, io.read, io.create, io.regular, io.sha, io.digest


def context(args):
    config = load_config().for_environment('container')
    paths = config.paths_for('fault-recovery', args.chain_id, args.candidate, 'alice', 'main', args.chain_id,
                             case='fault-recovery', date=args.date)
    return config, paths, {'experiment_id': 'fault-recovery', 'variant': args.chain_id, 'user_id': 'alice', 'session_id': 'main'}


def private(path, value):
    create(path, value)
    path.chmod(0o600)


def secret_file(path):
    value = secrets.token_urlsafe(32)
    descriptor = path.open('x')
    with descriptor as stream:
        path.chmod(0o600)
        stream.write(value + '\n')
    return value


def pins_for(args, paths, scope):
    pins = read(paths.run / 'trusted/pins.json')
    require(all(pins.get(key) == value for key, value in {
        'schema': 'harness-lab-fault-pins/1', 'candidate': args.candidate, 'chain_id': args.chain_id,
        'scope': scope, 'date': args.date, 'seed': args.seed, 'fault': args.fault,
        'write_mode': args.write_mode, 'model_call_cap': 20, 'active_limit_seconds': 300}.items()), 'fault_frozen_identity_mismatch')
    for key in ('candidate_image_id', 'bench_image_id'):
        require(isinstance(pins.get(key), str) and io.IMAGE.fullmatch(pins[key]), 'fault_invalid_image_pin')
    for key in ('source_export_sha256', 'adapter_export_sha256', 'fault_overlay_sha256'):
        require(isinstance(pins.get(key), str) and io.HEX.fullmatch(pins[key]), 'fault_invalid_source_pin')
    require(isinstance(pins.get('model_name'), str) and pins['model_name'], 'fault_model_missing')
    chain = read(paths.run / 'chain.json')
    require(chain['request_sha256'] == sha(regular(paths.fixture / 'candidate-request.json'))
            and chain['input_sha256'] == sha(regular(paths.run / 'input.json')), 'fault_original_input_changed')
    return pins


def expected_target(args, paths, scope):
    task, _ = detailed.task_definition('tools')
    wanted = expected_tools(args.seed, scope, task['workspace_inputs']['request.json'])
    require(wanted['selected'], 'fault_target_not_defined')
    return wanted['selected'][0]


def prepare(args, config, paths, scope):
    """只执行一次，包括从首次初始化就启用的非幂等 Fixture 模式。"""
    for path in (paths.run, paths.state, paths.workspace, paths.budget):
        require(not path.exists() and not path.is_symlink(), 'fault_chain_already_used')
    for path in (paths.run, paths.state, paths.workspace, paths.budget, paths.run / 'trusted'):
        path.mkdir(parents=True)
    for attempt in ('worker-a', 'worker-b'):
        (paths.run / 'attempts' / attempt / 'candidate').mkdir(parents=True)
        (paths.fixture / 'attempts' / attempt).mkdir(parents=True)
    task, task_hash = detailed.task_definition('tools')
    preexisting = ledger_request(paths, 'GET', '/admin/ledger', params={'experiment_id': scope['experiment_id'], 'variant': scope['variant']})
    require(preexisting.get('request_count') == preexisting.get('write_count') == 0 and preexisting.get('faults') == [], 'fault_scope_not_fresh')
    setup = ledger_request(paths, 'POST', '/admin/scenarios', json={
        'experiment_id': scope['experiment_id'], 'variant': scope['variant'], 'seed': args.seed,
        'users': [{'user_id': 'alice', 'session_id': 'main'}], 'write_mode': args.write_mode})
    own = [row for row in setup['scopes'] if same_scope(row, scope)]
    require(len(own) == 1 and own[0]['write_mode'] == args.write_mode, 'fault_fixture_scope_or_mode_mismatch')
    with (paths.fixture / 'scope.token').open('x') as stream:
        (paths.fixture / 'scope.token').chmod(0o600)
        stream.write(own[0]['token'] + '\n')
    secret_file(paths.budget / 'run.token')
    private(paths.fixture / 'scope.json', scope)
    private(paths.fixture / 'tool-binding.json', {'fixture_url': 'http://fixtures:8080', 'fixture_token': own[0]['token'],
        'workspace': '/workspace', 'scope': scope, 'events_path': '/evidence/tool-events.jsonl'})
    public = prepare_tools_inputs(paths.workspace, scope, task['workspace_inputs']['request.json'])
    prompt = '本任务可信作用域为 ' + json.dumps(scope, ensure_ascii=False) + '。\n' + public['prompt']
    request = {'run_id': args.chain_id, 'session_id': 'main', 'prompt': prompt,
        'workspace': '/workspace', 'state_dir': '/state', 'output_dir': '/evidence',
        'tools_url': 'http://tools:8081', 'tool_token_file': '/config/scope.token',
        'max_output_tokens': 4096, 'timeout_seconds': 300, 'max_retries': 2,
        'candidate_options': {'case': 'tools', 'trusted_scope_file': '/config/scope.json'}}
    private(paths.fixture / 'candidate-request.json', request)
    create(paths.run / 'input.json', {'candidate': args.candidate, 'chain_id': args.chain_id, 'scope': scope,
        'seed': args.seed, 'base_task': 'tools', 'task_sha256': task_hash, 'expected_answer_delivered': False,
        'active_limit_seconds': 300, 'model_call_cap': 20})
    private(paths.run / 'chain.json', {'schema': 'harness-lab-fault-chain/1', 'candidate': args.candidate,
        'chain_id': args.chain_id, 'scope': scope, 'seed': args.seed, 'fault': args.fault, 'write_mode': args.write_mode,
        'task_sha256': task_hash, 'input_sha256': sha(regular(paths.run / 'input.json')),
        'request_sha256': sha(regular(paths.fixture / 'candidate-request.json'))})
    # 宿主机固定文件为私有文件，在发出任何启动绑定前验证。
    private(paths.run / 'trusted/pins.json', read(paths.fixture / 'host-pins.json'))
    pins_for(args, paths, scope)
    if args.fault == 'kill_before_write':
        admin = secret_file(paths.fixture / 'prewrite-admin.token')
        attempts = {}
        for attempt in ('worker-a', 'worker-b'):
            attempts[attempt] = secret_file(paths.fixture / 'attempts' / attempt / 'prewrite.token')
            private(paths.fixture / 'attempts' / attempt / 'prewrite.json', {
                'schema': 'harness-lab-prewrite-client/1', 'chain_id': args.chain_id, 'scope': scope,
                'attempt_id': attempt, 'token_file': '/config/prewrite.token'})
        private(paths.fixture / 'prewrite-service.json', {'schema': 'harness-lab-prewrite-service/1',
            'chain_id': args.chain_id, 'scope': scope, 'database': '/private/prewrite.sqlite', 'admin_token_sha256': sha(admin.encode())})
        private(paths.fixture / 'prewrite-setup.json', {'gate_id': args.chain_id, 'chain_id': args.chain_id, 'scope': scope,
            'target': {'attempt_id': 'worker-a', 'operation_id': expected_target(args, paths, scope)['operation_id']},
            'attempts': {key: sha(value.encode()) for key, value in attempts.items()}})
        (paths.fixture / 'prewrite-private').mkdir(mode=0o700)


def gate_request(paths, method, suffix='', **kwargs):
    token = regular(paths.fixture / 'prewrite-admin.token').decode().strip()
    with httpx.Client(timeout=5, trust_env=False) as client:
        reply = client.request(method, 'http://tools:8081/experiment/admin/prewrite' + suffix,
            headers={'Authorization': 'Bearer ' + token}, **kwargs)
        reply.raise_for_status()
        return io.decode(reply.content)


def configure(args, paths, scope):
    target = expected_target(args, paths, scope)
    if args.fault in {'fail_once', 'drop_receipt_once'}:
        receipt = ledger_request(paths, 'POST', '/admin/faults', json={**scope,
            'operation_id': target['operation_id'], 'fault': args.fault})
        require(receipt.get('consumed') == 0, 'fault_was_already_consumed')
    elif args.fault == 'kill_before_write':
        receipt = gate_request(paths, 'POST', '/setup', json=read(paths.fixture / 'prewrite-setup.json'))
        require(receipt.get('configured') is True and receipt.get('gate_id') == args.chain_id, 'fault_gate_not_armed')
    else:
        receipt = {'proxy_inject_timeout_at': [1] if args.fault == 'model_timeout' else [], 'business_fault': 'none'}
    private(paths.run / 'trusted/fault-setup.json', {'scope': scope, 'chain_id': args.chain_id, 'fault': args.fault,
        'target_operation': target['operation_id'], 'receipt': receipt, 'prepared_at': io.now()})


def native_gap(paths, name, message, *, binding=None):
    private(paths.run / 'trusted' / name, {'schema': 'harness-lab-native-fault-gap/1', 'status': 'not_implemented',
        'reason': message, 'new_prompt_submitted': False, 'business_actions_performed_by_bench': False,
        **(binding or {})})
    return False


def bind_start(args, paths, scope, pins):
    binder = NATIVE_START_BINDERS.get(args.candidate)
    if binder is None:
        return native_gap(paths, 'native-start-gap.json', 'I23 同运行时启动 binder 尚未完成源码审查和校准')
    # 已审查 binder 写入原生请求、控制和 Worker 绑定。
    # 它必须只使用一次精确原始提示、选定的原生运行时变体，
    # 保持工具、模型和重试设置不变，并使用全新空原生状态目录。
    binder(args, paths, scope, pins)
    return True


def native_tree(root):
    """独立候选入口验证同一组有大小限制的文件事实。"""
    index = io.inventory(root, root)
    return {row['path']: {'bytes': row['bytes'], 'sha256': row['sha256']}
            for row in index['files'] if row['root'] == 'state'}


def decode_checkpoint(data):
    """读取固定序列化器数据，不导入或重建 SDK 类。

    仅投影固定的消息、增量快照和 Overwrite 数据。
    它们是静态字典，不是 SDK 对象或 Python 构造器。
    其他扩展值均明确保留为不可解释的哈希。"""
    import ormsgpack
    def extension(code, payload):
        if code == 1:
            record = ormsgpack.unpackb(payload, ext_hook=extension, option=ormsgpack.OPT_NON_STR_KEYS)
            if (isinstance(record, list) and len(record) == 3
                    and record[:2] == ['langgraph.types', 'Send']
                    and isinstance(record[2], list) and len(record[2]) == 2
                    and isinstance(record[2][0], str) and record[2][0]):
                return {'native_send': {'node': record[2][0], 'arg': record[2][1], 'timeout': None}}
        if code == 7:
            return {'native_delta_snapshot': ormsgpack.unpackb(payload, ext_hook=extension,
                option=ormsgpack.OPT_NON_STR_KEYS)}
        if code == 5:
            record = ormsgpack.unpackb(payload, ext_hook=extension, option=ormsgpack.OPT_NON_STR_KEYS)
            classes = {'langchain_core.messages.' + kind: name for kind, name in (
                ('human', 'HumanMessage'), ('ai', 'AIMessage'), ('tool', 'ToolMessage'),
                ('system', 'SystemMessage'), ('modifier', 'RemoveMessage'))}
            if (isinstance(record, list) and len(record) == 4 and record[0] in classes
                    and record[1] == classes[record[0]]
                    and isinstance(record[2], dict) and record[3] == 'model_validate_json'):
                return {'native_class': record[0] + '.' + record[1], 'fields': record[2]}
        if code == 2:
            record = ormsgpack.unpackb(payload, ext_hook=extension, option=ormsgpack.OPT_NON_STR_KEYS)
            if (isinstance(record, list) and len(record) == 3 and record[:2] == ['langgraph.types', 'Overwrite']
                    and isinstance(record[2], dict) and 'value' in record[2]
                    and not record[2].keys() - {'value', 'type'}
                    and record[2].get('type', '__overwrite__') == '__overwrite__'):
                return {'native_overwrite': record[2]['value']}
        return {'opaque_extension': code, 'bytes': len(payload), 'sha256': sha(payload)}
    return ormsgpack.unpackb(data, ext_hook=extension, option=ormsgpack.OPT_NON_STR_KEYS)


def native_json_projection(value):
    """复现固定入口的 JSON 表示，不构造对象。"""
    if isinstance(value, list):
        return [native_json_projection(item) for item in value]
    if isinstance(value, dict):
        if set(value) == {'native_class', 'fields'}:
            return native_json_projection(value['fields'])
        if set(value) == {'native_delta_snapshot'}:
            # _DeltaSnapshot 是 NamedTuple。json.dumps 在调用
            # 入口 default 编码器前先看到元组，因此输出 [value]。
            return [native_json_projection(value['native_delta_snapshot'])]
        require('opaque_extension' not in value and 'opaque_type' not in value,
                'fault_unsupported_native_public_projection')
        return {key: native_json_projection(item) for key, item in value.items()}
    return value


def message_record(value):
    """仅支持固定工具图发出的完整消息。"""
    classes = {'human': 'human.HumanMessage', 'ai': 'ai.AIMessage', 'tool': 'tool.ToolMessage',
        'system': 'system.SystemMessage', 'remove': 'modifier.RemoveMessage'}
    require(isinstance(value, dict) and set(value) == {'native_class', 'fields'}
            and isinstance(value['fields'], dict), 'fault_unsupported_native_message')
    fields = value['fields']
    require(fields.get('type') in classes
            and value['native_class'] == 'langchain_core.messages.' + classes[fields['type']]
            and (fields.get('id') is None or isinstance(fields['id'], str)), 'fault_invalid_native_message')
    return value


def message_overwrite(value):
    if isinstance(value, dict):
        if set(value) == {'native_overwrite'}:
            return True, value['native_overwrite']
        if set(value) == {'__overwrite__'}:
            return True, value['__overwrite__']
        if value.get('type') == '__overwrite__' and 'value' in value:
            return True, value['value']
    return False, None


def message_state(value):
    require(isinstance(value, list), 'fault_unsupported_native_message_state')
    return [message_record(item) for item in value]


def replay_message_writes(seed, values, *, live=False):
    """在静态消息上复现固定 DA reducer，不用作执行状态。

    祖先重放使用最后一次 Overwrite 及后续全部写入。
    单个运行中超步以 DeltaChannel.update 唯一的 Overwrite 为完整值。
    未知的类消息输入一律拒绝，不猜测 SDK 强制转换。"""
    base, start, overwrites = message_state(seed), 0, []
    for index, value in enumerate(values):
        overwrite, replacement = message_overwrite(value)
        if overwrite:
            overwrites.append(index)
            base, start = message_state([] if replacement is None else replacement), index + 1
    if live and overwrites:
        require(len(overwrites) == 1, 'fault_multiple_native_message_overwrites')
        return base
    flat = [message_record(item) for value in values[start:]
        for item in (value if isinstance(value, list) else [value])]
    resets = [index for index, row in enumerate(flat)
        if row['fields'].get('type') == 'remove' and row['fields'].get('id') == '__remove_all__']
    if resets:
        base, flat = [], flat[resets[-1] + 1:]
    result, positions = [], {}
    for row in base:
        key = row['fields'].get('id')
        if key is not None:
            positions[key] = len(result)
        result.append(row)
    for row in flat:
        key = row['fields'].get('id')
        if key is None:
            result.append(row)
        elif row['fields']['type'] == 'remove':
            if key in positions:
                result[positions.pop(key)] = None
        elif key in positions:
            result[positions[key]] = row
        else:
            positions[key] = len(result)
            result.append(row)
    return [row for row in result if row is not None]


def sqlite_writes(db, scope, checkpoint_id, *, messages_only=False):
    rows = []
    query = ('SELECT task_id,task_path,idx,channel,type,value FROM writes '
        'WHERE thread_id=? AND checkpoint_ns=? AND checkpoint_id=?'
        + (' AND channel=\'messages\'' if messages_only else '') + ' ORDER BY task_path,task_id,idx')
    for task, path, index, channel, kind, value in db.execute(query, (scope['session_id'], '', checkpoint_id)):
        require(isinstance(task, str) and isinstance(path, str) and type(index) is int
                and isinstance(channel, str) and isinstance(value, bytes), 'fault_invalid_pending_write_blob')
        require(not messages_only or kind == 'msgpack', 'fault_unsupported_native_message_encoding')
        rows.append({'task_id': task, 'task_path': path, 'index': index, 'channel': channel,
            'type': kind, 'bytes': len(value), 'sha256': sha(value),
            'value': decode_checkpoint(value) if kind == 'msgpack' else
                None if kind == 'null' and value == b'' else {'opaque_type': kind}})
    require(len({(row['task_id'], row['index']) for row in rows}) == len(rows), 'fault_duplicate_native_write_key')
    return rows


def sqlite_messages(db, scope, all_rows, head):
    """复现固定 saver 的父链遍历，排除目标自身的待处理日志。"""
    by_id = {row[0]: row for row in all_rows}
    require(len(by_id) == len(all_rows) and len(by_id) <= 4096, 'fault_native_checkpoint_history_bound')
    checkpoint_id = head['id']
    cursor, seen, ancestors, seed, seeded, stored = checkpoint_id, set(), [], [], None, None
    current = head
    while True:
        require(cursor not in seen and cursor in by_id, 'fault_invalid_native_parent_chain')
        seen.add(cursor)
        values = current.get('channel_values')
        require(isinstance(values, dict), 'fault_invalid_native_checkpoint_value')
        if 'messages' in values:
            stored = values['messages']
            seed = stored['native_delta_snapshot'] if isinstance(stored, dict) and set(stored) == {'native_delta_snapshot'} else stored
            seed = message_state(seed)
            seeded = cursor
            break
        if cursor == checkpoint_id:
            require('messages' in current.get('channel_versions', {}), 'fault_original_prompt_not_persisted')
        parent = by_id[cursor][1]
        if parent is None:
            break
        require(isinstance(parent, str) and parent in by_id and parent not in seen, 'fault_invalid_native_parent_chain')
        row = by_id[parent]
        require(row[2] == 'msgpack' and isinstance(row[3], bytes), 'fault_unsupported_native_checkpoint_encoding')
        current = decode_checkpoint(row[3])
        require(isinstance(current, dict) and current.get('id') == parent, 'fault_invalid_native_checkpoint_value')
        cursor = parent
        ancestors.append(cursor)
    # 种子属于其自身步骤起点，也包含该步骤的写入；
    # 目标待处理写入特意不列入此表。
    ordered = list(reversed(ancestors))
    history = [{'checkpoint_id': cid, 'writes': sqlite_writes(db, scope, cid, messages_only=True)} for cid in ordered]
    messages = replay_message_writes(seed, [row['value'] for step in history for row in step['writes']])
    return messages, {'schema': 'harness-lab-native-message-history/1',
        'mode': 'stored_head' if seeded == checkpoint_id else 'ancestor_delta_writes',
        'seed_checkpoint_id': seeded, 'ancestor_checkpoint_ids': ordered, 'steps': history,
        'seed_storage': stored, 'seed_messages': seed,
        'seed_checkpoint_blob_sha256': sha(by_id[seeded][3]) if seeded is not None else None,
        'target_pending_writes_excluded': True, 'reconstructed_messages_sha256': digest(messages)}


def natural_sqlite_audit(paths, scope, original):
    """精确排除 A 后，检查包含 WAL 的私有字节副本。

    绝不通过 SQLite 打开原库：只读连接仍可能触碰 SHM。
    此副本仅供诊断，绝不挂载到 B。"""
    before = io.inventory(paths.state, paths.workspace)
    state = {row['path']: row for row in before['files'] if row['root'] == 'state'}
    require('checkpoints.sqlite' in state and 'pause-index.json' not in state, 'fault_natural_db_missing_or_safe_pause_substituted')
    require(not any(name.endswith('-journal') and row['bytes'] for name, row in state.items()), 'fault_hot_rollback_journal_unsupported')
    with tempfile.TemporaryDirectory(prefix='i23-sqlite-observation-') as temporary:
        database = Path(temporary) / 'checkpoints.sqlite'
        for suffix in ('', '-wal', '-shm', '-journal'):
            name = database.name + suffix
            if name in state:
                copied = regular(paths.state / name)
                require(sha(copied) == state[name]['sha256'], 'fault_sqlite_bytes_changed_before_copy')
                (Path(temporary) / name).write_bytes(copied)
        with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
            db.execute('PRAGMA query_only=ON')
            db.execute('BEGIN')
            require(db.execute('PRAGMA quick_check').fetchall() == [('ok',)], 'fault_natural_sqlite_not_consistent')
            checkpoint_columns = {row[1] for row in db.execute('PRAGMA table_info(checkpoints)')}
            write_columns = {row[1] for row in db.execute('PRAGMA table_info(writes)')}
            require(checkpoint_columns == {'thread_id', 'checkpoint_ns', 'checkpoint_id', 'parent_checkpoint_id', 'type', 'checkpoint', 'metadata'}
                and write_columns == {'thread_id', 'checkpoint_ns', 'checkpoint_id', 'task_id', 'task_path', 'idx', 'channel', 'type', 'value'},
                'fault_unrecognized_native_sqlite_schema')
            require(db.execute('SELECT DISTINCT thread_id,checkpoint_ns FROM checkpoints').fetchall() == [(scope['session_id'], '')],
                    'fault_foreign_thread_or_namespace')
            foreign = db.execute('SELECT COUNT(*) FROM writes WHERE thread_id!=? OR checkpoint_ns!=?', (scope['session_id'], '')).fetchone()[0]
            require(foreign == 0, 'fault_foreign_pending_writes')
            all_rows = db.execute('SELECT checkpoint_id,parent_checkpoint_id,type,checkpoint,metadata FROM checkpoints '
                'WHERE thread_id=? AND checkpoint_ns=? ORDER BY checkpoint_id', (scope['session_id'], '')).fetchall()
            require(all_rows, 'fault_no_naturally_persisted_checkpoint')
            checkpoint_id, parent, kind, blob, metadata = all_rows[-1]
            require(kind == 'msgpack' and isinstance(blob, bytes), 'fault_unsupported_native_checkpoint_encoding')
            decoded = decode_checkpoint(blob)
            require(isinstance(decoded, dict) and decoded.get('id') == checkpoint_id
                    and isinstance(decoded.get('channel_values'), dict), 'fault_invalid_native_checkpoint_value')
            messages, history = sqlite_messages(db, scope, all_rows, decoded)
            require(messages, 'fault_original_prompt_not_persisted')
            humans = [row['fields'] for row in messages if isinstance(row, dict)
                and row.get('native_class') == 'langchain_core.messages.human.HumanMessage']
            require(len(humans) == 1 and humans[0].get('content') == original['prompt'], 'fault_crash_not_original_user_objective')
            pending = sqlite_writes(db, scope, checkpoint_id)
            heads = [{'checkpoint_id': row[0], 'parent_checkpoint_id': row[1], 'type': row[2],
                'checkpoint_sha256': sha(row[3]), 'metadata_sha256': sha(row[4]) if row[4] is not None else None} for row in all_rows]
    require(before == io.inventory(paths.state, paths.workspace), 'fault_observation_changed_original_native_files')
    return {'schema': 'harness-lab-natural-sqlite-audit/1', 'scope': scope, 'session_id': scope['session_id'],
        'checkpoint_ns': '', 'checkpoint_id': checkpoint_id, 'parent_checkpoint_id': parent,
        'checkpoint_blob_sha256': sha(blob), 'checkpoint_history': heads, 'checkpoint': decoded,
        'messages': messages, 'message_history': history,
        'pending_writes': pending, 'pending_write_keys': sorted([row['task_id'], row['channel']] for row in pending),
        'original_prompt_sha256': sha(original['prompt'].encode()), 'native_inventory': before,
        'original_files_unchanged': True, 'observation_mode': '复制原始 SQLite/WAL/SHM 字节，在私有临时目录中使用只读事务',
        'execution_source': '使用原始候选状态，不使用此诊断副本', 'pause_index_used': False}


def worker_binding(paths, scope, pins, attempt, request, controls, *, argv, entrypoint):
    directory = paths.fixture / 'attempts' / attempt
    private(directory / 'request.json', request)
    private(paths.run / 'trusted' / (attempt + '-binding.json'), {
        'schema': 'harness-lab-fault-worker-binding/1', 'candidate': pins['candidate'], 'chain_id': pins['chain_id'],
        'scope': scope, 'attempt_id': attempt, 'mode': 'native-start' if attempt == 'worker-a' else 'native-crash-resume',
        'candidate_image_id': pins['candidate_image_id'], 'request_sha256': sha(regular(directory / 'request.json')),
        'model_token_sha256': sha(regular(paths.budget / 'run.token').strip()), 'scope_sha256': sha(regular(paths.fixture / 'scope.json')),
        'scope_token_sha256': sha(regular(paths.fixture / 'scope.token').strip()),
        'native_control_files': controls, 'control_sha256': {name: sha(regular(directory / name)) for name in controls},
        'entrypoint': entrypoint, 'argv': argv, 'pins_sha256': sha(regular(paths.run / 'trusted/pins.json'))})


def deepagents_binding(args, paths, scope, pins, boundary=None):
    original = read(paths.fixture / 'candidate-request.json')
    attempt = 'worker-b' if boundary is not None else 'worker-a'
    target = paths.fixture / 'attempts' / attempt
    require(not list((paths.run / 'attempts' / attempt / 'candidate').iterdir()), 'fault_attempt_output_not_fresh')
    if boundary is None:
        require(not list(paths.state.iterdir()) and model_snapshot(paths, allow_empty=True)['count'] == 0, 'fault_start_not_empty')
        private(paths.run / 'trusted/http-tool-surface.json', {'tools': io.trusted_tools(paths)})
        used, recovery = 0, None
    else:
        host = read(paths.run / 'trusted/resume-clock.json')
        require(host.get('chain_id') == args.chain_id and host.get('scope') == scope
                and host.get('boundary_sha256') == sha(regular(paths.run / 'trusted/crash-boundary.json')),
                'fault_continuous_resume_budget_missing')
        used = host['used_active_seconds']
        require(io.number(used) and used < 300, 'fault_continuous_resume_budget_invalid')
        audit = natural_sqlite_audit(paths, scope, original)
        hit = read(paths.run / 'trusted/gate-hit.json')
        hit_events = [row for row in hit.get('events', []) if row.get('phase') == 'prewrite_hit']
        actual_calls = [call for message in audit['messages']
            if isinstance(message, dict) and message.get('native_class') == 'langchain_core.messages.ai.AIMessage'
            for call in message['fields'].get('tool_calls', [])]
        wanted = expected_target(args, paths, scope)
        arguments = {key: wanted[key] for key in ('operation_id', 'item_id', 'quantity')}
        require(hit_events and all(isinstance(event.get('native_call_id'), str) and event['native_call_id']
                and sum(call.get('id') == event['native_call_id'] and call.get('name') == 'lab_write'
                        and call.get('args') == arguments for call in actual_calls) == 1 for event in hit_events),
                'fault_gate_call_not_in_original_natural_checkpoint')
        audit['gate_to_native_calls'] = [{'gate_event_id': event['event_id'], 'bridge_call_id': event['bridge_call_id'],
            'native_tool_call_id': event['native_call_id'], 'source': '独立物理 AIMessage.tool_calls 与持久化可信预写入命中进行比较'}
            for event in hit_events]
        private(paths.run / 'trusted/native-crash-sqlite.json', audit)
        recovery = {'boundary_sha256': sha(regular(paths.run / 'trusted/crash-boundary.json')),
            'audit_sha256': sha(regular(paths.run / 'trusted/native-crash-sqlite.json')), 'checkpoint_id': audit['checkpoint_id'],
            'pending_write_keys': audit['pending_write_keys'], 'state_files': native_tree(paths.state), 'workspace_files': native_tree(paths.workspace)}
    remaining = math.floor(300 - used)
    require(remaining >= 1, 'fault_continuous_budget_exhausted')
    request = {**original, 'timeout_seconds': remaining, 'fault_recovery': {'mode': 'crash-resume' if boundary else 'new',
        'chain_id': args.chain_id, 'attempt_id': attempt, 'binding_file': '/config/native-crash.json'}}
    if boundary is not None:
        request.pop('prompt')
    binding = {'schema': 'harness-lab-crash-binding/1', 'candidate': args.candidate,
        **request['fault_recovery'], 'scope': scope, 'session_id': scope['session_id'], 'checkpoint_ns': '',
        'source_export_sha256': pins['source_export_sha256'], 'adapter_export_sha256': pins['adapter_export_sha256'],
        'runtime_sha256': io.da_runtime(paths, pins, original), 'candidate_image_id': pins['candidate_image_id'],
        'model_token_sha256': sha(regular(paths.budget / 'run.token').strip()),
        'budget': {'active_limit_seconds': 300, 'used_active_seconds': used, 'remaining_seconds': remaining}, 'recovery': recovery}
    binding.pop('binding_file')
    private(target / 'native-crash.json', binding)
    worker_binding(paths, scope, pins, attempt, request, ['native-crash.json'], entrypoint='/opt/venv/bin/python',
        argv=['/opt/materials/harness-lab/fault_entrypoint.py', '--request', '/config/request.json'])


def deepagents_start(args, paths, scope, pins):
    deepagents_binding(args, paths, scope, pins)


NATIVE_START_BINDERS['deepagents_langgraph'] = deepagents_start
NATIVE_CRASH_BINDERS['deepagents_langgraph'] = deepagents_binding


def deepseek_binding(args, paths, scope, pins, boundary=None):
    """A、B 和无故障基线使用同一隔离原生 Goal 运行时。"""
    original = read(paths.fixture / 'candidate-request.json')
    attempt = 'worker-b' if boundary is not None else 'worker-a'
    target = paths.fixture / 'attempts' / attempt
    require(not list((paths.run / 'attempts' / attempt / 'candidate').iterdir()), 'fault_attempt_output_not_fresh')
    # 宿主机必须在绑定前将无模型校准冻结到 pins。
    # 此处重新计算替代预期哈希，会错误接受已变化字节。
    contract_hash = io.dsh_contract(paths, pins, scope)
    recovery, used = None, 0
    if boundary is None:
        require(not list(paths.state.iterdir()) and model_snapshot(paths, allow_empty=True)['count'] == 0, 'fault_start_not_empty')
    else:
        clock = read(paths.run / 'trusted/resume-clock.json')
        require(clock.get('chain_id') == args.chain_id and clock.get('scope') == scope
                and clock.get('boundary_sha256') == sha(regular(paths.run / 'trusted/crash-boundary.json')),
                'fault_continuous_resume_budget_missing')
        used = clock.get('used_active_seconds')
        require(io.number(used) and used < 300, 'fault_continuous_resume_budget_invalid')
        audit = deepseek_crash_observation(paths, scope, pins)
        goal = audit['durable_goal']
        require(goal['phase'] in {'active', 'paused', 'blocked'} and 1 <= goal['roundsStarted'] < 20
                and goal['maxGoalRounds'] == 20, 'fault_dsh_original_goal_not_resumable')
        hit = read(paths.run / 'trusted/gate-hit.json')
        hits = [row for row in hit.get('events', []) if row.get('phase') == 'prewrite_hit']
        wanted = expected_target(args, paths, scope)
        arguments = {key: wanted[key] for key in ('operation_id', 'item_id', 'quantity')}
        calls = [(event, block) for event in audit['events'] if event['type'] == 'assistant/message'
            for block in event['data']['message']['content'] if block.get('type') == 'tool-call']
        require(hits and all(isinstance(event.get('native_call_id'), str) and event['native_call_id']
                and sum(block.get('id') == event['native_call_id'] and block.get('name') == 'write'
                    and io.decode(block['arguments'].encode()) == arguments for _, block in calls) == 1 for event in hits),
                'fault_dsh_gate_call_not_in_original_natural_session')
        audit['gate_to_native_calls'] = [{'gate_event_id': event['event_id'], 'bridge_call_id': event['bridge_call_id'],
            'native_tool_call_id': event['native_call_id'],
            'assistant_event_seq': next(row['seq'] for row, block in calls if block['id'] == event['native_call_id']),
            'durable_tool_call_seqs': [row['seq'] for row in audit['events'] if row['type'] == 'tool/call'
                and row['data'].get('callId') == event['native_call_id']],
            'source': '独立原始物理 assistant 工具调用与可信持久化预写入命中进行比较'} for event in hits]
        private(paths.run / 'trusted/native-crash-dsh.json', audit)
        recovery = {key: audit[key] for key in ('physical_path', 'physical_bytes', 'physical_sha256', 'committed_bytes',
            'committed_prefix_sha256', 'events_sha256', 'next_seq', 'expected_repaired_events_sha256',
            'expected_repaired_next_seq', 'durable_goal')}
        recovery.update({'boundary_sha256': sha(regular(paths.run / 'trusted/crash-boundary.json')),
            'audit_sha256': sha(regular(paths.run / 'trusted/native-crash-dsh.json'))})
    remaining = math.floor(300 - used)
    require(remaining >= 1, 'fault_continuous_budget_exhausted')
    private(target / 'fault-state.json', io.inventory(paths.state, paths.workspace))
    request = {**original, 'timeout_seconds': remaining, 'fault_recovery': {
        'mode': 'crash-resume' if boundary is not None else 'new', 'chain_id': args.chain_id,
        'attempt_id': attempt, 'binding_file': '/config/fault-goal.json'}}
    if boundary is not None:
        request.pop('prompt')
    model = {'provider': 'deepseek-official', 'base_url': 'http://model-proxy:8082/v1', 'model': pins['model_name'],
        'max_output_tokens': original['max_output_tokens'], 'max_retries': original['max_retries'], 'thinking': 'disabled', 'reasoning_effort': 'off'}
    tools = {'url': original['tools_url'], 'scope': scope, 'token_sha256': sha(regular(paths.fixture / 'scope.token').strip())}
    binding = {'schema': 'harness-lab-dsh-crash-binding/1', 'candidate': args.candidate, 'variant': 'fault-goal-runtime',
        'mode': request['fault_recovery']['mode'], 'chain_id': args.chain_id, 'attempt_id': attempt,
        'scope': scope, 'session_id': scope['session_id'],
        'materials': {key: pins[key] for key in ('source_export_sha256', 'adapter_export_sha256')},
        'image': {'original_image_id': pins['candidate_image_id']},
        'model': {'config_sha256': digest(model), 'run_token_sha256': sha(regular(paths.budget / 'run.token').strip()),
            'budget_id': args.chain_id, 'max_model_calls': 20},
        'tools': {'config_sha256': digest(tools), 'contract_sha256': contract_hash},
        'state': {'index_file': '/config/fault-state.json', 'index_sha256': sha(regular(target / 'fault-state.json'))},
        'goal': {'max_goal_rounds': 20}, 'active_budget': {'limit_seconds': 300, 'used_seconds': used, 'remaining_seconds': remaining}}
    if recovery is not None:
        binding['recovery'] = recovery
    private(target / 'fault-goal.json', binding)
    worker_binding(paths, scope, pins, attempt, request, ['fault-goal.json', 'fault-state.json'], entrypoint='node',
        argv=['--import', 'tsx/esm', '/opt/materials/harness-lab/fault-goal-entrypoint.ts', '--request', '/config/request.json'])


def deepseek_start(args, paths, scope, pins):
    deepseek_binding(args, paths, scope, pins)


def dsh_interrupted_closers(events):
    """独立以纯数据复现固定公共 Session repair.ts。

    这些事件仅用于预期证据，bench 绝不将其写入原生状态。
    agents.resume 必须实际执行修复并持久化记录。"""
    turn, step, pending = None, None, {}
    for event in events:
        kind, details = event['type'], event.get('data', {})
        if kind == 'turn/start':
            turn, step, pending = details['turn'], None, {}
        elif kind == 'turn/end':
            turn, step, pending = None, None, {}
        elif kind == 'step/start':
            step = details['step']
        elif kind == 'step/end':
            step, pending = None, {}
        elif kind == 'assistant/message':
            for block in details['message']['content']:
                if block.get('type') == 'tool-call':
                    pending[block['id']] = {'step': details['step']}
        elif kind == 'tool/call' and details['callId'] in pending:
            pending[details['callId']]['call_seq'] = event['seq']
        elif kind == 'tool/result':
            pending.pop(details['message']['source']['callId'], None)
    if turn is None or not events:
        return []
    seq, stamp, closers = events[-1]['seq'] + 1, events[-1]['time'], []
    for call_id, item in pending.items():
        started = 'call_seq' in item
        # 以下英文须精确复现固定上游的修复事件，不作为本仓库的新提示。
        text = ('The tool call was interrupted after it was recorded, but no result was durably recorded. Its outcome is unknown. '
            'Decide whether to retry from the tool semantics: retry only if the operation is read-only or idempotent; '
            'if it may have side effects, first verify external state or ask the user. Do not retry blindly.' if started else
            'The tool call was interrupted before the Harness recorded it as started. Retry it if it is still needed.')
        event = {'type': 'tool/result', 'seq': seq, 'time': stamp, 'data': {'turn': turn, 'step': item['step'],
            'message': {'id': f'interrupted-tool-result-{call_id}-{seq}', 'role': 'user', 'source': {'kind': 'tool', 'callId': call_id},
                'content': [{'type': 'tool-result', 'toolCallId': call_id, 'isError': True,
                    'content': [{'type': 'text', 'text': text}]}]},
            'error': {'name': 'ToolOutcomeUnknownError' if started else 'ToolNotStartedError',
                'code': 'TOOL_OUTCOME_UNKNOWN' if started else 'TOOL_NOT_STARTED'}}, 'surfaceOp': 'append'}
        if started:
            event['sourceEventSeqs'] = [item['call_seq']]
        closers.append(event)
        seq += 1
    if step is not None:
        closers.append({'type': 'step/end', 'seq': seq, 'time': stamp, 'data': {'turn': turn, 'step': step}})
        seq += 1
    closers.append({'type': 'turn/end', 'seq': seq, 'time': stamp, 'data': {'turn': turn, 'reason': {'kind': 'interrupted'}}})
    return closers


def dsh_physical_prefix(data, scope):
    """仅支持严格明文、未打包格式，保留撕裂的末尾片段。

    固定原生扫描器也会丢弃格式错误的完整尾行。
    首轮实验对此类行一律拒绝，不扩展可接受的修复范围；
    这类链属于已记录的持久性缺口。"""
    end = data.rfind(b'\n') + 1
    require(end > 0, 'fault_dsh_header_not_committed')
    records = [io.decode(row) for row in data[:end].splitlines()]
    require(records and records[0].get('type') == 'session' and records[0].get('id') == scope['session_id']
            and records[0].get('cwd') == '/workspace', 'fault_dsh_foreign_physical_header')
    events = [io.expand_dsh_provenance(row) for row in records[1:]]
    require(all(type(row.get('seq')) is int and row['seq'] == index and isinstance(row.get('type'), str)
                and 'time' in row and isinstance(row.get('data'), dict) for index, row in enumerate(events)),
            'fault_dsh_partial_or_noncontiguous_prefix')
    return records[0], events, end


def deepseek_crash_observation(paths, scope, pins):
    """读取原始字节，不加载 Session、不修复、不额外 flush 或写入。"""
    original = read(paths.fixture / 'candidate-request.json')
    before = io.inventory(paths.state, paths.workspace)
    require(not any(row['root'] == 'state' and ('pause-index' in row['path'] or 'lifecycle-pause' in row['path'])
                for row in before['files']), 'fault_dsh_safe_pause_substituted')
    entry = read(paths.run / 'attempts/worker-a/candidate/entrypoint.json')
    require(entry.get('scope') == scope and entry.get('profile') == 'harness-lab-fault-goal'
            and entry.get('variant') == 'fault-goal-runtime' and entry.get('session_action') == 'create'
            and entry.get('session_id') == scope['session_id']
            and all(entry.get('provenance', {}).get(key) == pins[key] for key in ('source_export_sha256', 'adapter_export_sha256')),
            'fault_dsh_original_profile_or_source_missing')
    candidates = list((paths.state / 'sessions').rglob('*.jsonl'))
    require(candidates, 'fault_dsh_original_session_not_persisted')
    require(len(candidates) == 1, 'fault_dsh_original_session_ambiguous')
    physical = candidates[0]
    data = regular(physical)
    header, events, committed = dsh_physical_prefix(data, scope)
    goal = fold_durable_goal(events)
    require(goal is not None, 'fault_dsh_original_goal_not_persisted')
    require(goal.get('objective') == original['prompt'], 'fault_dsh_original_goal_objective_changed')
    closers = dsh_interrupted_closers(events)
    turn, calls, sources, splices = None, {}, [], []
    for row in events:
        kind, details = row['type'], row['data']
        if kind == 'turn/start':
            turn, calls, sources, splices = details.get('turn'), {}, [], []
        elif kind == 'turn/end':
            turn, calls, sources, splices = None, {}, [], []
        elif kind == 'user/message':
            sources.append({'seq': row['seq'], 'source': details.get('source')})
        elif kind == 'agent/inbox/spliced':
            splices.append({'seq': row['seq'], 'data': details})
        elif kind == 'tool/call':
            calls[details['callId']] = {'seq': row['seq'], 'data': details}
        elif kind == 'tool/result':
            calls.pop(details.get('message', {}).get('source', {}).get('callId'), None)
    observed = {'schema': 'harness-lab-natural-dsh-audit/1', 'scope': scope, 'chain_id': scope['variant'],
        'physical_path': physical.relative_to(paths.state).as_posix(), 'physical_bytes': len(data), 'physical_sha256': sha(data),
        'committed_bytes': committed, 'committed_prefix_sha256': sha(data[:committed]),
        'discarded_fragment_bytes': len(data) - committed, 'discarded_fragment_sha256': sha(data[committed:]),
        'native_inventory': before, 'partial_trailing_line': committed < len(data), 'header': header,
        'native_session_loaded': False, 'native_repair_invoked': False, 'activation_inferred': False,
        'pause_index_used': False, 'extra_flush_requested': False, 'durable_goal': goal, 'events': events,
        'next_seq': len(events), 'events_sha256': digest(events), 'expected_repair_events': closers,
        'expected_repaired_next_seq': len(events) + len(closers), 'expected_repaired_events_sha256': digest(events + closers),
        'unended_turn': turn, 'recorded_tools_without_durable_result': calls,
        'user_message_sources_in_open_tail': sources, 'actual_inbox_splices_in_open_tail': splices,
        'claim_meaning': '仅使用实际 agent/inbox/spliced 的 removedCount/outcome/inserted 记录描述领取；user/message 不算领取',
        'boundary': '仅使用自然持久化前缀，不推断缓冲事件或进程本地激活状态'}
    require(before == io.inventory(paths.state, paths.workspace), 'fault_dsh_crash_observation_changed_native_state')
    return observed


NATIVE_START_BINDERS['deepseek_harness'] = deepseek_start
NATIVE_CRASH_BINDERS['deepseek_harness'] = deepseek_binding


def validated_gate(args, paths, scope):
    snapshot = gate_request(paths, 'GET')
    require(snapshot.get('schema') == 'harness-lab-prewrite-status/1'
            and snapshot.get('scope') == scope and snapshot.get('chain_id') == args.chain_id, 'foreign_fault_gate')
    gate = snapshot.get('gate', {})
    target = expected_target(args, paths, scope)
    require(gate.get('gate_id') == args.chain_id and gate.get('target_attempt') == 'worker-a'
            and gate.get('target_operation') == target['operation_id'], 'fault_gate_target_substituted')
    events = snapshot.get('events', [])
    require(events and all(type(row.get('event_id')) is int and row['event_id'] == index
            and row.get('scope') == scope and row.get('chain_id') == args.chain_id
            for index, row in enumerate(events, 1)), 'fault_gate_event_history_invalid')
    target_path = paths.run / 'trusted/prewrite-gate.jsonl'
    previous = [row for _, _, row in io.jsonl(target_path)] if target_path.exists() else []
    require(events[:len(previous)] == previous, 'fault_durable_gate_event_prefix_changed')
    descriptor = os.open(target_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'ab') as stream:
        stream.write(b''.join(io.canonical(row) + b'\n' for row in events[len(previous):]))
        stream.flush()
        os.fsync(stream.fileno())
    return snapshot


def observe_gate(args, paths, scope):
    snapshot = validated_gate(args, paths, scope)
    if snapshot['gate']['state'] != 'hit':
        return False
    hits = [row for row in snapshot['events'] if row.get('phase') == 'prewrite_hit']
    target = expected_target(args, paths, scope)
    wanted = {key: target[key] for key in ('operation_id', 'item_id', 'quantity')}
    require(hits and snapshot['gate']['first_hit_event'] == hits[0]['event_id']
            and all(row.get('attempt_id') == 'worker-a' and row.get('operation_id') == target['operation_id']
                    and row.get('arguments_sha256') == digest(wanted) for row in hits), 'fault_gate_hit_wrong_arguments')
    private(paths.run / 'trusted/gate-hit.json', snapshot)
    # 宿主机只消费持久化来源身份，不消费预期业务答案。
    private(paths.run / 'trusted/gate-signal.json', {'chain_id': args.chain_id, 'scope': scope, 'attempt_id': 'worker-a',
        'snapshot_sha256': sha(regular(paths.run / 'trusted/gate-hit.json')), 'hit_event_id': hits[0]['event_id'],
        'observed_at': io.now(), 'observed': True})
    return True


def killed_worker(record, pins, scope):
    """外部 SIGKILL137 只证明故障注入，不能算健康进程退出。"""
    require(record.get('schema') == 'harness-lab-fault-worker/1' and record.get('attempt_id') == 'worker-a'
            and record.get('chain_id') == pins['chain_id'] and record.get('scope') == scope, 'fault_kill_foreign_worker')
    before, live, after = (record.get(key, {}) for key in ('before_start', 'before_kill', 'after_exit'))
    identity = before.get('id')
    require(isinstance(identity, str) and io.HEX.fullmatch(identity) and identity == live.get('id') == after.get('id'), 'fault_kill_wrong_id')
    require(all(row.get('image_id') == pins['candidate_image_id'] and row.get('name') == '/' + pins['worker_names']['worker-a']
                for row in (before, live, after)), 'fault_kill_wrong_image_or_name')
    require(before.get('state', {}).get('Status') == 'created' and before['state'].get('Running') is False
            and live.get('state', {}).get('Running') is True and type(live['state'].get('Pid')) is int
            and live['state']['Pid'] > 0, 'fault_not_live_at_exact_kill')
    killed = record.get('kill', {})
    require(killed.get('signal') == 'SIGKILL' and killed.get('container_id') == identity
            and type(killed.get('exit_code')) is int and killed['exit_code'] == 0, 'fault_actual_kill_missing')
    state = after.get('state', {})
    require(state.get('Status') == 'exited' and type(state.get('Pid')) is int and state['Pid'] == 0
            and type(state.get('ExitCode')) is int and state['ExitCode'] == 137
            and all(state.get(key) is False for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
            and state.get('Error') == '', 'fault_kill_not_proven_non_oom_exit137')
    deleted = record.get('deleted', {})
    require(deleted.get('id_absent') is True and deleted.get('name_absent') is True, 'fault_old_worker_not_deleted')
    require(io.instant(record['started_at']) <= io.instant(killed['sent_at']) <= io.instant(record['finished_at'])
            <= io.instant(deleted['observed_at']), 'fault_kill_lifecycle_out_of_order')
    return record


def model_snapshot(paths, *, allow_empty=False):
    """I23 允许真实失败；与 I22 不同，injected_timeout 在预期内。"""
    token_hash = sha(regular(paths.budget / 'run.token').strip())
    database = paths.budget / 'calls.sqlite'
    regular(database)
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2) as db:
        db.execute('PRAGMA query_only=ON')
        db.execute('BEGIN')
        binding = db.execute('SELECT id,token_hash,cap FROM budget ORDER BY id').fetchall()
        rows = [list(row) for row in db.execute('SELECT id,started_at,path,outcome,upstream_status FROM calls ORDER BY id')]
    require(binding == [(1, token_hash, 20)] and len(rows) <= 20 and (rows or allow_empty)
            and all(type(row[0]) is int and row[0] == index for index, row in enumerate(rows, 1)), 'fault_model_budget_binding_or_count')
    allowed = {'reserved', 'forwarded', 'injected_timeout', 'upstream_timeout', 'upstream_error', 'client_disconnected'}
    require(all(row[3] in allowed for row in rows), 'fault_unknown_proxy_outcome')
    return {'cap': 20, 'token_sha256': token_hash, 'count': len(rows), 'rows': rows,
            'rows_sha256': digest(rows), 'in_flight': [row[0] for row in rows if row[3] == 'reserved']}


def after_kill(args, paths, scope, pins):
    record = killed_worker(read(paths.run / 'trusted/worker-a.json'), pins, scope)
    signal = read(paths.run / 'trusted/gate-signal.json')
    require(signal.get('observed') is True and signal.get('scope') == scope and signal.get('chain_id') == args.chain_id
            and signal.get('snapshot_sha256') == sha(regular(paths.run / 'trusted/gate-hit.json'))
            and record['kill'].get('gate_signal_sha256') == sha(regular(paths.run / 'trusted/gate-signal.json'))
            and io.instant(signal['observed_at']) <= io.instant(record['kill']['sent_at']), 'fault_kill_without_bound_durable_hit')
    gate_request(paths, 'POST', '/abandon', json={'gate_id': args.chain_id, 'attempt_id': 'worker-a'})
    snapshot = validated_gate(args, paths, scope)
    deadline = time.monotonic() + 5
    # 仅等待持久化准入的任务由其原始处理器完成。
    # 对本控制器而言，未知就是终态：不改成已知，也不重试。
    while (snapshot.get('unsettled_dispatches') and time.monotonic() < deadline
            and all(row.get('state') == 'admitted' for row in snapshot['unsettled_dispatches'])):
        time.sleep(0.1)
        snapshot = validated_gate(args, paths, scope)
    private(paths.run / 'trusted/gate-after-kill.json', snapshot)
    # 采取保守规则：不手工解决未知或已准入调用。宿主机
    # 只有在独立观测到在途调用正常完成后，才能调用此函数。
    # 未结束的记录使整条链以已记录的未知状态结束。
    require(snapshot['gate']['state'] == 'abandoned' and snapshot.get('unsettled_dispatches') == [], 'fault_A_requests_not_quiescent')
    ledger = ledger_request(paths, 'GET', '/admin/ledger', params={'experiment_id': 'fault-recovery', 'variant': args.chain_id})
    require(scoped_ledger(ledger, scope), 'fault_before_recovery_ledger_invalid')
    private(paths.run / 'trusted/ledger-before-recovery.json', ledger)
    events = [row for _, _, row in io.jsonl(paths.run / 'tool-events.jsonl')]
    complete, calls = audited_calls(events, scope)
    require(complete, 'fault_A_tool_audit_incomplete')
    operation = expected_target(args, paths, scope)['operation_id']
    require(not any(start['tool'] == 'write' and start['arguments'].get('operation_id') == operation for start, _ in calls)
            and not any(row.get('action') == 'write' and row.get('operation_id') == operation for row in ledger['requests'])
            and not any(row.get('operation_id') == operation for row in ledger['writes']), 'fault_target_was_already_dispatched')
    budget = model_snapshot(paths)
    require(not budget['in_flight'], 'fault_model_requests_not_quiescent')
    private(paths.run / 'trusted/crash-boundary.json', {'schema': 'harness-lab-crash-boundary/1', 'scope': scope,
        'chain_id': args.chain_id, 'worker_id': record['after_exit']['id'], 'gate_snapshot_sha256': sha(regular(paths.run / 'trusted/gate-after-kill.json')),
        'ledger_sha256': sha(regular(paths.run / 'trusted/ledger-before-recovery.json')),
        'tool_audit': {'sha256': sha(regular(paths.run / 'tool-events.jsonl')), 'lines': len(events),
                       'bytes': len(regular(paths.run / 'tool-events.jsonl'))},
        'model_budget': budget, 'natural_native_inventory': io.inventory(paths.state, paths.workspace),
        'native_state_explicitly_flushed': False, 'pause_index_used': False, 'recorded_at': io.now()})
    return True


def bind_crash_resume(args, paths, scope, pins):
    boundary = read(paths.run / 'trusted/crash-boundary.json')
    require(boundary.get('scope') == scope and boundary.get('chain_id') == args.chain_id
            and boundary.get('pause_index_used') is False and boundary.get('native_state_explicitly_flushed') is False,
            'fault_crash_boundary_missing')
    require(boundary['natural_native_inventory'] == io.inventory(paths.state, paths.workspace)
            and boundary['model_budget'] == model_snapshot(paths), 'fault_native_or_budget_changed_before_B')
    gap_binding = {'candidate': args.candidate, 'scope': scope, 'chain_id': args.chain_id,
        'candidate_image_id': pins.get('candidate_image_id'),
        'crash_boundary_sha256': sha(regular(paths.run / 'trusted/crash-boundary.json'))}
    binder = NATIVE_CRASH_BINDERS.get(args.candidate)
    if binder is None:
        return native_gap(paths, 'native-recovery-gap.json', '未登记经过源码审查的崩溃恢复入口；未创建 B，也未重发原始输入', binding=gap_binding)
    # 只能在此 bench 镜像检查 A 自然持久化的状态；固定
    # 原始 thread/Goal/Session、待处理写入及运行时身份。
    # 不能靠候选自报索引、SIGKILL 后 flush、恢复安全暂停索引、
    # 新 Goal 或模型/工具调用伪造缺失进展。
    try:
        binder(args, paths, scope, pins, boundary)
    except ValueError as error:
        # 缺失自然持久化任务属于测得的原生能力缺口。
        # 身份或来源损坏、证据变化仍一律拒绝。
        if args.candidate == 'deepseek_harness' and str(error) in {
                'fault_dsh_original_session_not_persisted', 'fault_dsh_original_goal_not_persisted',
                'fault_dsh_original_goal_not_resumable'}:
            return native_gap(paths, 'native-recovery-gap.json', str(error) + '；未创建 B、新提示或替代 Goal', binding=gap_binding)
        raise
    return True


def operation_analysis(ledger, events, scope, before=None):
    valid, calls = audited_calls(events, scope)
    require(valid and scoped_ledger(ledger, scope), 'fault_operation_sources_invalid')
    before_ids = {row['id'] for row in (before or {}).get('requests', [])}
    operations = sorted({row.get('operation_id') for row in ledger['requests'] if row.get('action') == 'write'}
                        | {row['operation_id'] for row in ledger['writes']})
    result = []
    for operation in operations:
        requests = [row for row in ledger['requests'] if row.get('action') == 'write' and row.get('operation_id') == operation]
        writes = [row for row in ledger['writes'] if row.get('operation_id') == operation]
        first_commit = next((index for index, row in enumerate(requests) if row.get('status') in {0, 201}), None)
        reissued = requests[first_commit + 1:] if first_commit is not None else []
        tool_writes = [start for start, _ in calls if start['tool'] == 'write' and start['arguments'].get('operation_id') == operation]
        queries = []
        for start, finish in calls:
            if start['tool'] not in {'receipt', 'ledger'} or finish.get('status') != 200:
                continue
            payload = finish.get('result', {})
            observed = payload.get('receipts', []) if start['tool'] == 'receipt' else payload.get('writes', [])
            matching = [row for row in observed if isinstance(row, dict) and same_scope(row, scope)
                        and row.get('operation_id') == operation and any(
                            all(row.get(key) == actual.get(key) for key in ('id', 'operation_id', 'item_id', 'quantity', 'total_cents')) for actual in writes)]
            if matching:
                queries.append({'server_call_id': start['call_id'], 'tool': start['tool'], 'finish_time': finish['time'],
                                'matched_write_ids': [row['id'] for row in matching], 'origin': 'candidate_or_model_native_call；bench 未查询'})
        result.append({'operation_id': operation, 'tool_write_calls': len(tool_writes), 'business_request_count': len(requests),
            'actual_write_count': len(writes), 'request_ids': [row['id'] for row in requests],
            'request_statuses': [row['status'] for row in requests], 'write_ids': [row['id'] for row in writes],
            'by_attempt': {'worker-a': sum(row['id'] in before_ids for row in requests),
                           'worker-b': sum(row['id'] not in before_ids for row in requests)} if before is not None else {'worker-a': len(requests)},
            'reissued_after_commit_request_ids': [row['id'] for row in reissued], 'actual_duplicate': len(writes) > 1,
            'service_prevented_duplicate': bool(reissued) and len(writes) == 1 and all(row.get('mode') == 'idempotent' for row in writes),
            'successful_result_queries': queries,
            'query_before_first_reissue': bool(reissued) and any(io.instant(row['finish_time']) < io.instant(reissued[0]['created_at']) for row in queries),
            'decision_origin': 'not_inferred_from_query_name_or_final_report'})
    return result


def injection_valid(args, paths, scope, ledger, budget):
    if args.fault == 'none':
        require(not any(row.get('fault') != 'none' for row in ledger['faults'])
                and not any(row[3] == 'injected_timeout' for row in budget['rows']), 'fault_in_baseline')
        return True
    target = expected_target(args, paths, scope)['operation_id']
    if args.fault == 'model_timeout':
        require(budget['rows'] and budget['rows'][0][0] == 1 and budget['rows'][0][3] == 'injected_timeout'
                and sum(row[3] == 'injected_timeout' for row in budget['rows']) == 1, 'fault_model_timeout_not_injected_once')
    elif args.fault in {'fail_once', 'drop_receipt_once'}:
        faults = [row for row in ledger['faults'] if row.get('operation_id') == target]
        requests = [row for row in ledger['requests'] if row.get('action') == 'write' and row.get('operation_id') == target]
        code = 503 if args.fault == 'fail_once' else 0
        require(len(faults) == 1 and faults[0].get('fault') == args.fault and faults[0].get('consumed') == 1
                and requests and requests[0].get('fault') == args.fault and requests[0].get('status') == code,
                'fault_business_window_not_hit')
        if args.fault == 'drop_receipt_once':
            require(any(row.get('operation_id') == target for row in ledger['writes']), 'fault_drop_without_actual_commit')
    else:
        boundary = read(paths.run / 'trusted/crash-boundary.json')
        require(boundary.get('scope') == scope and boundary.get('chain_id') == args.chain_id
                and boundary.get('pause_index_used') is False, 'fault_kill_window_unproven')
    return True


def controlled_failure_native(paths, scope, candidate, attempt, facts):
    """仅在 bench 中解码 DA 的原始物理终态消息。

    宿主机标准库读取器固定 BLOB 哈希；只有独立 bench 导入固定的安全 MessagePack 解码器。
    不重建 SDK 类，也不把候选框架快照当作物理检查点。"""
    if candidate != 'deepagents_langgraph':
        return {'physical_native_failure_verified': True, 'reader': 'bound_public_dsh_jsonl'}
    directory = paths.run / 'attempts' / attempt / 'candidate'
    snapshot = read(directory / 'state-snapshot.json')
    original = read(paths.fixture / 'candidate-request.json')
    audit = natural_sqlite_audit(paths, scope, original)
    physical = facts.get('native_checkpoint', {})
    require(audit['checkpoint_id'] == physical.get('checkpoint_id')
            and audit['checkpoint_blob_sha256'] == physical.get('checkpoint_blob_sha256')
            and audit['checkpoint_history'][-1]['metadata_sha256'] == physical.get('metadata_sha256'),
            'fault_controlled_failure_physical_head_changed')
    messages = audit['messages']
    projected = snapshot.get('values', {}).get('messages')
    require(isinstance(projected, list) and len(messages) == len(projected) and bool(messages)
            and all(isinstance(native, dict) and native.get('fields') == public
                and native.get('native_class') == 'langchain_core.messages.' + public.get('type', '') + '.' + {
                    'human': 'HumanMessage', 'ai': 'AIMessage', 'tool': 'ToolMessage', 'system': 'SystemMessage'}.get(public.get('type'), '')
                for native, public in zip(messages, projected)), 'fault_controlled_failure_physical_messages_differ')
    return {'physical_native_failure_verified': True, 'reader': 'independent_bench_safe_msgpack_projection',
        'checkpoint_id': audit['checkpoint_id'], 'checkpoint_blob_sha256': audit['checkpoint_blob_sha256'],
        'metadata_sha256': physical['metadata_sha256'], 'original_files_unchanged': True}


def process_evidence(paths, scope, pins, args):
    """独立检查精确镜像、挂载和进程；137 仍属于注入。"""
    config = load_config().for_environment('container')
    records = {}
    for attempt in ('worker-a', 'worker-b'):
        file = paths.run / 'trusted' / (attempt + '.json')
        if not file.exists():
            require(attempt == 'worker-b', 'fault_worker_a_evidence_missing')
            continue
        row, binding = read(file), read(paths.run / 'trusted' / (attempt + '-binding.json'))
        directory = paths.fixture / 'attempts' / attempt
        require(row.get('schema') == 'harness-lab-fault-worker/1' and row.get('scope') == scope
                and row.get('chain_id') == args.chain_id and row.get('attempt_id') == attempt
                and row.get('errors') == [] and row.get('timed_out') is False
                and row.get('binding_sha256') == sha(regular(paths.run / 'trusted' / (attempt + '-binding.json'))),
                'fault_worker_lifecycle_identity_invalid')
        require(binding.get('request_sha256') == sha(regular(directory / 'request.json'))
                and binding.get('scope_sha256') == sha(regular(paths.fixture / 'scope.json'))
                and binding.get('scope_token_sha256') == sha(regular(paths.fixture / 'scope.token').strip())
                and binding.get('model_token_sha256') == sha(regular(paths.budget / 'run.token').strip())
                and binding.get('pins_sha256') == sha(regular(paths.run / 'trusted/pins.json'))
                and all(binding['control_sha256'].get(name) == sha(regular(directory / name)) for name in binding['native_control_files']),
                'fault_original_protected_controls_changed')
        request = read(directory / 'request.json')
        original = read(paths.fixture / 'candidate-request.json')
        require(request.get('run_id') == args.chain_id and request.get('session_id') == scope['session_id']
                and (request.get('prompt') == original['prompt'] if attempt == 'worker-a' else
                     'prompt' not in request and 'followup' not in request and 'followup' not in request.get('candidate_options', {})),
                'fault_original_input_reissued_or_substituted')
        expected = {'/config/request.json': (directory / 'request.json', False), '/config/scope.json': (paths.fixture / 'scope.json', False),
            '/config/scope.token': (paths.fixture / 'scope.token', False), '/budget/run.token': (paths.budget / 'run.token', False),
            '/state': (paths.state, True), '/workspace': (paths.workspace, True),
            '/evidence': (paths.run / 'attempts' / attempt / 'candidate', True)}
        allowed_controls = {'native-crash.json'} if args.candidate == 'deepagents_langgraph' else {'fault-goal.json', 'fault-state.json'}
        require(set(binding['native_control_files']) == allowed_controls, 'fault_unreviewed_native_mount')
        expected.update({'/config/' + name: (directory / name, False) for name in allowed_controls})
        if args.fault == 'kill_before_write':
            expected.update({'/config/' + name: (directory / name, False) for name in ('prewrite.json', 'prewrite.token')})
        before, after = row.get('before_start', {}), row.get('after_exit', {})
        for snapshot in (before, after):
            require(snapshot.get('id') == before.get('id') and isinstance(snapshot.get('id'), str)
                    and io.HEX.fullmatch(snapshot['id']) and snapshot.get('image_id') == pins['candidate_image_id']
                    and snapshot.get('image_tag') == pins['candidate_image_id']
                    and snapshot.get('name') == '/' + pins['worker_names'][attempt]
                    and snapshot.get('memory_limit') == 2 * 1024**3 and snapshot.get('cpu_nano') == 10**9,
                    'fault_worker_actual_identity_or_caps_changed')
            mounts = snapshot.get('mounts', [])
            by_dest = {item.get('Destination'): item for item in mounts}
            require(len(by_dest) == len(mounts) and set(by_dest) == set(expected)
                    and all(by_dest[dest].get('Source') == io.source_on_host(config, path)
                            and by_dest[dest].get('RW') is writable for dest, (path, writable) in expected.items()),
                    'fault_worker_actual_mounts_invalid')
        require(before.get('state', {}).get('Status') == 'created' and before['state'].get('Running') is False,
                'fault_worker_creation_not_observed')
        if attempt == 'worker-a' and args.fault == 'kill_before_write':
            killed_worker(row, pins, scope)
            require(type(row.get('attach_exit')) is int and row['attach_exit'] == 137
                    and row.get('process_exit') == 137, 'fault_kill_attach_or_process_mismatch')
        else:
            state = after.get('state', {})
            code = state.get('ExitCode')
            require(type(code) is int and code in {0, 1}
                    and type(row.get('process_exit')) is int and type(row.get('attach_exit')) is int
                    and row.get('process_exit') == row.get('attach_exit') == code
                    and state.get('Status') == 'exited' and type(state.get('Pid')) is int and state['Pid'] == 0
                    and all(state.get(key) is False for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
                    and state.get('Error') == '' and row.get('kill') is None, 'fault_normal_worker_unhealthy')
            if code == 1:
                row['controlled_failure'] = controlled_failure(paths.run, paths.fixture, scope, pins,
                    args.candidate, attempt, after, state=paths.state)
                row['controlled_failure_native'] = controlled_failure_native(paths, scope, args.candidate,
                    attempt, row['controlled_failure'])
        records[attempt] = row
    if 'worker-b' in records:
        require(records['worker-a']['after_exit']['id'] != records['worker-b']['after_exit']['id']
                and io.instant(records['worker-a']['deleted']['observed_at']) <= io.instant(records['worker-b']['started_at']),
                'fault_worker_b_started_before_exact_a_deleted')
    return records


def control_exit_expected(paths, scope, pins, processes, record):
    """明确的未恢复诊断不等于控制程序崩溃。"""
    code = record.get('exit_code')
    if type(code) is int and code == 0:
        return True
    if type(code) is not int or code != 3 or record.get('action') != 'bind-crash-resume':
        return False
    gap = read(paths.run / 'trusted/native-recovery-gap.json')
    boundary_path = paths.run / 'trusted/crash-boundary.json'
    boundary = read(boundary_path)
    actual = record.get('after_exit', {})
    registrations = [read(path) for path in (paths.run / 'trusted').glob('bench-registration-*.json')]
    matches = [item for item in registrations if item.get('id') == actual.get('id')]
    if len(matches) != 1:
        return False
    registration = matches[0]
    created = registration.get('before_start', {})
    return (gap.get('schema') == 'harness-lab-native-fault-gap/1'
        and gap.get('status') == 'not_implemented' and gap.get('scope') == scope
        and gap.get('chain_id') == pins['chain_id'] == scope['variant']
        and gap.get('candidate') == pins.get('candidate')
        and io.IMAGE.fullmatch(str(pins.get('candidate_image_id', '')))
        and gap.get('candidate_image_id') == pins['candidate_image_id']
        and gap.get('crash_boundary_sha256') == sha(regular(boundary_path))
        and boundary.get('scope') == scope and boundary.get('chain_id') == pins['chain_id']
        and boundary.get('pause_index_used') is False and boundary.get('native_state_explicitly_flushed') is False
        and registration.get('action') == 'bind-crash-resume'
        and registration.get('scope') == scope and registration.get('chain_id') == pins['chain_id']
        and registration.get('crash_boundary_sha256') == gap['crash_boundary_sha256']
        and registration.get('name') == record.get('name') == str(actual.get('name', '')).removeprefix('/')
        and registration.get('image_id') == created.get('image_id') == actual.get('image_id') == pins['bench_image_id']
        and created.get('id') == actual.get('id') and created.get('name') == actual.get('name')
        and created.get('state', {}).get('Status') == 'created'
        and created.get('state', {}).get('Running') is False
        and gap.get('new_prompt_submitted') is False and gap.get('business_actions_performed_by_bench') is False
        and 'worker-b' not in processes
        and not any((paths.run / 'trusted' / name).exists() or (paths.run / 'trusted' / name).is_symlink()
            for name in ('worker-b-binding.json', 'worker-b-created.json', 'worker-b.json')))


def resource_evidence(paths, scope, pins, processes):
    """不补零：原始 stats 必须与精确观测的生命周期转换相符。"""
    import math
    from runner.metrics import memory_bytes
    orchestration = read(paths.run / 'trusted/orchestration-final.json')
    require(orchestration.get('errors') == [] and orchestration.get('timed_out') is False
            and orchestration.get('scope') == scope and orchestration.get('chain_id') == pins['chain_id']
            and io.number(orchestration.get('used_active_seconds')) and orchestration['used_active_seconds'] <= 300,
            'fault_active_window_or_host_error')
    begin, end = io.instant(orchestration['active_started_at']), io.instant(orchestration['active_finished_at'])
    require(begin <= end and abs((end - begin) - orchestration['used_active_seconds']) < 1,
            'fault_continuous_wall_window_inconsistent')
    controlled = {}
    for attempt, worker in processes.items():
        if 'controlled_failure' not in worker:
            continue
        facts = controlled_failure(paths.run, paths.fixture, scope, pins, pins['candidate'],
            attempt, worker['after_exit'], state=paths.state)
        verified = controlled_failure_native(paths, scope, pins['candidate'], attempt, facts)
        require(facts == worker['controlled_failure'] and verified == worker.get('controlled_failure_native'),
                'fault_controlled_failure_sources_changed_before_resources')
        controlled[attempt] = {'evidence': facts, 'native_verification': verified}
    services = read(paths.run / 'trusted/service-host.json')
    require(set(services) == {'fixtures', 'tools', 'model-proxy'}, 'fault_shared_service_inventory_invalid')
    known = {row['before_start']['id']: 'worker' for row in processes.values()}
    limits = {row['before_start']['id']: (2 * 1024**3, 10**9) for row in processes.values()}
    for name, item in services.items():
        require(item.get('image_id') == pins['services_image_id'] and item.get('state', {}).get('Running') is True
                and item.get('name') == '/harness-lab-' + name + '-1'
                and item.get('memory_limit') == 256 * 1024**2 and item.get('cpu_nano') == 250000000,
                'fault_shared_service_image_or_cap_changed')
        known[item['id']] = 'shared_service'
        limits[item['id']] = (256 * 1024**2, 250000000)
    control_records = {}
    for path in sorted((paths.run / 'trusted').glob('bench-process-*.json')):
        item = read(path)
        snapshot = item.get('after_exit', {})
        require(snapshot.get('image_id') == pins['bench_image_id'] and snapshot.get('memory_limit') == 512 * 1024**2
                and snapshot.get('cpu_nano') == 500000000
                and snapshot.get('state', {}).get('Status') == 'exited'
                and type(snapshot.get('state', {}).get('Pid')) is int and snapshot['state']['Pid'] == 0
                and all(snapshot.get('state', {}).get(key) is False
                    for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
                and snapshot.get('state', {}).get('Error') == ''
                and snapshot.get('state', {}).get('ExitCode') == item.get('exit_code')
                and control_exit_expected(paths, scope, pins, processes, item),
                'fault_control_container_unhealthy')
        known[snapshot['id']] = 'bench'
        limits[snapshot['id']] = (512 * 1024**2, 500000000)
        control_records[snapshot['id']] = item
    samples = [row for _, _, row in io.jsonl(paths.run / 'resource-samples.jsonl')]
    require(len(samples) >= 2, 'fault_insufficient_resource_samples')
    previous, peaks = None, {'allocated_memory_bytes': 0, 'allocated_cpu_nano': 0, 'observed_memory_bytes': 0, 'observed_cpu_percent': 0}
    seen_workers = set()
    for sample in samples:
        at = io.instant(sample['at'])
        require(sample.get('error') is None and isinstance(sample.get('worker_ids'), list), 'fault_resource_sample_error')
        require(previous is None or 0 < at - previous <= 5, 'fault_resource_sampling_gap')
        previous = at
        rows = sample.get('containers', [])
        raw = [io.decode(line.encode()) for line in sample.get('docker_stats_raw', '').splitlines()]
        raw_names = {row.get('Name'): row for row in raw}
        require(len(raw_names) == len(raw) and len({row.get('id') for row in rows}) == len(rows), 'fault_duplicate_stats')
        missing = set(raw_names) - {row.get('Name') for row in rows}
        actual_worker_ids = {identity for identity, role in known.items() if role == 'worker'}
        require(not missing and len(sample['worker_ids']) <= 2
                and len(set(sample['worker_ids'])) == len(sample['worker_ids'])
                and set(sample['worker_ids']) <= actual_worker_ids,
                'fault_unaccounted_stats_or_worker_count')
        for row in rows:
            require(row.get('id') in known and row.get('role') == known[row['id']]
                    and row.get('Name') in raw_names
                    and all(row.get(key) == value for key, value in raw_names[row['Name']].items()),
                    'fault_stats_not_bound_to_exact_container')
            require(type(row.get('memory_limit')) is int and row['memory_limit'] > 0
                    and type(row.get('cpu_nano')) is int and row['cpu_nano'] > 0
                    and (row['memory_limit'], row['cpu_nano']) == limits[row['id']], 'fault_missing_resource_caps')
            if row['role'] == 'worker':
                seen_workers.add(row['id'])
        for transition in sample.get('lifecycle', []):
            before, after = transition.get('before', {}), transition.get('after', {})
            state = after.get('state', {})
            normal = transition.get('event') == 'exited_during_sample' and type(state.get('ExitCode')) is int and state['ExitCode'] == 0
            control = control_records.get(after.get('id'))
            diagnostic = (transition.get('event') == 'diagnostic_exit_during_sample'
                and type(state.get('ExitCode')) is int and state['ExitCode'] == 3
                and before.get('state', {}).get('Running') is True
                and control is not None and control.get('action') == transition.get('control_action') == 'bind-crash-resume'
                and control.get('exit_code') == 3 and control.get('after_exit') == after
                and transition.get('registered_name') == str(after.get('name', '')).removeprefix('/')
                and transition.get('gap_sha256') == sha(regular(paths.run / 'trusted/native-recovery-gap.json'))
                and transition.get('crash_boundary_sha256') == sha(regular(paths.run / 'trusted/crash-boundary.json'))
                and control_exit_expected(paths, scope, pins, processes, control))
            failed = False
            if transition.get('event') == 'controlled_failure_during_sample':
                attempt = transition.get('attempt_id')
                worker = processes.get(attempt, {})
                require(before.get('state', {}).get('Running') is True
                        and after.get('id') == worker.get('after_exit', {}).get('id')
                        and worker.get('after_exit') == after
                        and type(worker.get('process_exit')) is int and type(worker.get('attach_exit')) is int
                        and worker.get('process_exit') == worker.get('attach_exit') == 1,
                        'fault_controlled_failure_transition_not_original_worker')
                failure = controlled_failure(paths.run, paths.fixture, scope, pins,
                    pins['candidate'], attempt, after, state=paths.state)
                require(transition.get('controlled_failure') == failure == worker.get('controlled_failure'),
                        'fault_controlled_failure_transition_sources_changed')
                failed = True
            require((normal or diagnostic or failed) and before.get('id') == after.get('id') == transition.get('id')
                    and before.get('image_id') == after.get('image_id') and before.get('name') == after.get('name')
                    and after.get('id') in known and state.get('Status') == 'exited' and state.get('Pid') == 0
                    and all(state.get(key) is False for key in ('Running', 'OOMKilled', 'Dead', 'Paused', 'Restarting'))
                    and state.get('Error') == '', 'fault_missing_stats_without_exact_healthy_exit')
        allocated_memory = sum(row['memory_limit'] for row in rows)
        allocated_cpu = sum(row['cpu_nano'] for row in rows)
        observed_memory = sum(memory_bytes(row['MemUsage'].split('/')[0]) for row in rows)
        observed_cpu = sum(float(row['CPUPerc'].rstrip('%')) for row in rows)
        require(allocated_memory <= 6 * 1024**3 and allocated_cpu <= 3 * 10**9
                and math.isfinite(observed_memory) and 0 <= observed_memory <= 6 * 1024**3
                and math.isfinite(observed_cpu) and 0 <= observed_cpu <= 300, 'fault_total_resource_limit_exceeded')
        for key, value in zip(peaks, (allocated_memory, allocated_cpu, observed_memory, observed_cpu)):
            peaks[key] = max(peaks[key], value)
    worker_ids = {row['before_start']['id'] for row in processes.values()}
    require(seen_workers == worker_ids and io.instant(samples[0]['at']) <= begin + 3
            and io.instant(samples[-1]['at']) >= end - 3, 'fault_resource_samples_do_not_cover_active_workers')
    result = {'passed': True, 'samples_sha256': sha(regular(paths.run / 'resource-samples.jsonl')), 'sample_count': len(samples),
        'peaks': peaks, 'kill_exit137_is_healthy_exit': False}
    if controlled:
        result['controlled_failures'] = controlled
    return result


def live_message_projection(audit, snapshot):
    """单独统计当前头检查点中实际已完成任务的写入。

    graph.aget_state(current-head config) 提供运行中视图，saver 则返回原始存储。
    先应用 NULL_TASK_ID 输入，再应用当前任务输出。
    不将待处理写入折叠进祖先/基线审计。"""
    tasks = snapshot.get('tasks')
    require(isinstance(tasks, list), 'fault_public_live_tasks_missing')
    registered = {}
    for task in tasks:
        if isinstance(task, list) and len(task) == 7:
            task = dict(zip(('id', 'name', 'path', 'error', 'interrupts', 'state', 'result'), task))
        require(isinstance(task, dict) and isinstance(task.get('id'), str)
                and task['id'] not in registered and isinstance(task.get('path'), list)
                and not task.get('error') and not task.get('interrupts'), 'fault_public_live_task_invalid')
        registered[task['id']] = task
    def task_path(value):
        require(type(value) in (str, int, bool, list), 'fault_public_live_task_path_invalid')
        if isinstance(value, list):
            return '~' + ', '.join(task_path(item) for item in value)
        # 固定调度器会在 PUSH 路径追加 False。其 task_path_str
        # 使用 isinstance(int)，故有意将 bool 也格式化为 0/1。
        return f'{value:010d}' if isinstance(value, int) else value
    paths = [task_path(task['path']) for task in registered.values()]
    require(len(set(paths)) == len(paths), 'fault_public_live_task_path_ambiguous')
    pending = [row for row in audit['pending_writes'] if row['channel'] == 'messages']
    null_id = '00000000-0000-0000-0000-000000000000'
    null, outputs = [], []
    for row in pending:
        require(row['type'] == 'msgpack', 'fault_unsupported_native_message_encoding')
        if row['task_id'] == null_id:
            null.append(row['value'])
            continue
        task = registered.get(row['task_id'])
        require(task is not None and task_path(task['path']) == row['task_path']
                and task.get('result') is not None, 'fault_pending_message_not_current_finished_task')
        outputs.append(row['value'])
    base = audit['messages']
    if null:
        base = replay_message_writes(base, null, live=True)
    return replay_message_writes(base, outputs, live=True) if outputs else base


def deepagents_resume_evidence(paths, scope, pins):
    binding = read(paths.fixture / 'attempts/worker-b/native-crash.json')
    audit = read(paths.run / 'trusted/native-crash-sqlite.json')
    boundary = read(paths.run / 'trusted/crash-boundary.json')
    result = read(paths.run / 'attempts/worker-b/candidate/native-crash-resume.json')
    require(audit.get('scope') == scope and audit.get('native_inventory') == boundary.get('natural_native_inventory')
            and audit.get('original_files_unchanged') is True and audit.get('pause_index_used') is False,
            'fault_independent_physical_crash_audit_invalid')
    require(result.get('schema') == 'harness-lab-native-crash-resume/1' and result.get('scope') == scope
            and result.get('chain_id') == pins['chain_id'] and result.get('attempt_id') == 'worker-b'
            and result.get('physical_audit_sha256') == sha(regular(paths.run / 'trusted/native-crash-sqlite.json'))
            and result.get('boundary_sha256') == sha(regular(paths.run / 'trusted/crash-boundary.json'))
            and binding['recovery']['audit_sha256'] == result['physical_audit_sha256']
            and result.get('new_prompt_submitted') is False and result.get('pause_index_used') is False
            and result.get('input', 'missing') is None, 'fault_native_crash_resume_not_original_binding')
    for value in (result.get('checkpoint_tuple', {}), result.get('snapshot_before_resume', {})):
        configurable = value.get('config', {}).get('configurable', {})
        require(configurable.get('thread_id') == scope['session_id'] and configurable.get('checkpoint_ns', '') == ''
                and configurable.get('checkpoint_id') == audit['checkpoint_id'], 'fault_public_native_head_differs_from_physical_audit')
    pending = result['checkpoint_tuple'].get('pending_writes')
    require(isinstance(pending, list) and all(isinstance(row, list) and len(row) == 3 for row in pending)
            and sorted([row[0], row[1]] for row in pending) == audit['pending_write_keys'] == result.get('pending_write_keys'),
            'fault_public_pending_writes_differ_from_physical_rows')
    public_checkpoint = result['checkpoint_tuple'].get('checkpoint', {})
    physical_values = audit['checkpoint']['channel_values']
    public_values = public_checkpoint.get('channel_values', {})
    require(isinstance(public_values, dict) and ('messages' in public_values) == ('messages' in physical_values)
            and ('messages' not in physical_values or public_values['messages'] == native_json_projection(physical_values['messages']))
            and all(public_checkpoint.get(key) == audit['checkpoint'].get(key)
                for key in ('id', 'channel_versions', 'versions_seen')),
            'fault_public_checkpoint_messages_differ_from_original_sqlite')
    physical_pending = [(row['task_id'], row['channel'], native_json_projection(row['value'])) for row in audit['pending_writes']]
    require(len(physical_pending) == len(pending) and all(
        sum([task, channel, value] == row for row in pending) == 1 for task, channel, value in physical_pending),
        'fault_public_pending_values_differ_from_original_sqlite')
    live = result['snapshot_before_resume']
    require(live.get('values', {}).get('messages') == native_json_projection(live_message_projection(audit, live)),
            'fault_public_live_messages_differ_from_original_sqlite')
    require(result.get('config') == {'configurable': {'thread_id': scope['session_id'], 'checkpoint_ns': ''}, 'recursion_limit': 64},
            'fault_resume_selected_historical_checkpoint')
    return True



def deepseek_resume_evidence(paths, scope, pins):
    """关联原始字节、实际原生修复、原始 Goal 和 B 日志。

    仅有自报哈希不能证明恢复。最终原始物理 JSONL 必须精确包含
    独立预期的前缀和原生关闭事件。"""
    directory = paths.run / 'attempts/worker-b/candidate'
    binding = read(paths.fixture / 'attempts/worker-b/fault-goal.json')
    recovery = binding.get('recovery', {})
    audit = read(paths.run / 'trusted/native-crash-dsh.json')
    boundary = read(paths.run / 'trusted/crash-boundary.json')
    result = read(directory / 'native-crash-resume.json')
    before = read(directory / 'session-before-followup.json')
    require(audit.get('scope') == scope and audit.get('chain_id') == pins['chain_id']
            and audit.get('native_inventory') == boundary.get('natural_native_inventory')
            and audit.get('native_session_loaded') is False and audit.get('native_repair_invoked') is False
            and audit.get('extra_flush_requested') is False and audit.get('pause_index_used') is False,
            'fault_dsh_independent_crash_audit_invalid')
    require(result.get('schema') == 'harness-lab-dsh-crash-resume/1' and result.get('scope') == scope
            and result.get('chain_id') == pins['chain_id'] and result.get('session_id') == scope['session_id']
            and result.get('audit_sha256') == recovery.get('audit_sha256') == sha(regular(paths.run / 'trusted/native-crash-dsh.json'))
            and result.get('boundary_sha256') == recovery.get('boundary_sha256') == sha(regular(paths.run / 'trusted/crash-boundary.json'))
            and result.get('pause_index_used') is False and result.get('new_prompt_submitted') is False
            and result.get('new_followup_submitted') is False, 'fault_dsh_resume_binding_not_original')
    require(binding.get('mode') == 'crash-resume' and binding.get('variant') == 'fault-goal-runtime'
            and binding.get('materials') == {key: pins[key] for key in ('source_export_sha256', 'adapter_export_sha256')}
            and binding.get('image', {}).get('original_image_id') == pins['candidate_image_id']
            and recovery.get('durable_goal') == audit['durable_goal'], 'fault_dsh_runtime_identity_changed')
    original_artifact = result.get('original_artifact', {})
    require(original_artifact.get('scope') == scope and original_artifact.get('physical_sha256') == audit['physical_sha256']
            and original_artifact.get('physical_bytes') == audit['physical_bytes'], 'fault_dsh_raw_pre_resume_not_original')
    expected = audit['events'] + dsh_interrupted_closers(audit['events'])
    require(audit['expected_repair_events'] == expected[len(audit['events']):]
            and digest(expected) == audit['expected_repaired_events_sha256'], 'fault_dsh_expected_repair_changed')
    # 物理 JSONL 的 tag 不属于公共 SessionHeader。唯一
    # 官方默认值为 delegationDepth=0；保留其他全部头字段。
    io.restored_dsh_header({key: value for key, value in audit['header'].items() if key != 'type'}, before.get('header'))
    events = before.get('events')
    needs_marker = not expected or expected[-1]['type'] != 'session/end-seed'
    require(before.get('scope') == scope and before.get('session_action') == 'resume'
            and before.get('header', {}).get('id') == scope['session_id'] and before['header'].get('cwd') == '/workspace'
            and before.get('agent_status') == 'idle' and before.get('inbox', {}).get('has_pending') is False
            and before.get('first_live_seq') == len(expected) and before.get('next_seq') == len(expected) + int(needs_marker)
            and isinstance(events, list) and len(events) == before['next_seq'] and events[:len(expected)] == expected,
            'fault_dsh_actual_restored_prefix_differs_from_original_and_native_repair')
    if needs_marker:
        marker = events[-1]
        require(marker.get('type') == 'session/end-seed' and marker.get('seq') == len(expected)
                and marker.get('data') == {}, 'fault_dsh_native_end_seed_missing')
    require(result.get('restored_goal') == {**audit['durable_goal'], 'activation': 'disarmed'}
            and result.get('restored_events_sha256') == digest(events)
            and result.get('original_prefix_sha256') == audit['events_sha256']
            and result.get('repair_events') == audit['expected_repair_events'], 'fault_dsh_restored_goal_or_repair_projection_invalid')
    physical = regular(paths.state / audit['physical_path'])
    header, final_events, committed = dsh_physical_prefix(physical, scope)
    require(committed == len(physical) and header == audit['header']
            and sha(physical[:audit['committed_bytes']]) == audit['committed_prefix_sha256']
            and final_events[:len(events)] == events, 'fault_dsh_original_committed_bytes_or_actual_repair_rewritten')
    new_changes = [event for event in final_events[len(events):] if event['type'] == 'goal/change']
    require(new_changes and new_changes[0]['data'].get('operation') == 'resume'
            and new_changes[0]['data'].get('goal', {}).get('id') == audit['durable_goal']['id']
            and new_changes[0]['data']['goal'].get('revision') == audit['durable_goal']['revision'] + 1,
            'fault_dsh_original_goal_not_rearmed_via_resume')
    final_goal = fold_durable_goal(final_events)
    require(final_goal and all(final_goal[key] == audit['durable_goal'][key]
                for key in ('id', 'objective', 'maxGoalRounds')), 'fault_dsh_replacement_goal_created')
    raw = [io.native_dsh(row) for _, _, row in io.jsonl(directory / 'raw-events.jsonl')]
    require(all(type(event.get('seq')) is int and 0 <= event['seq'] < len(final_events)
                and event == final_events[event['seq']] for event in raw)
            and new_changes[0] in raw, 'fault_dsh_resume_native_event_not_in_physical_log')
    observations = [row for _, _, row in io.jsonl(directory / 'fault-events.jsonl')
                    if row.get('phase') == 'native_original_goal_resume_returned']
    require(len(observations) == 1 and observations[0].get('scope') == scope
            and observations[0].get('attempt_id') == 'worker-b'
            and observations[0].get('original_goal_id') == audit['durable_goal']['id']
            and observations[0].get('original_goal_revision') == audit['durable_goal']['revision']
            and matches_live_view(observations[0].get('goal'), fold_durable_goal(final_events[:new_changes[0]['seq'] + 1]), 'armed'),
            'fault_dsh_public_goal_resume_observation_missing')
    uncertainty = [{'native_call_id': event['data']['message']['source']['callId'], 'native_error': event['data']['error'],
        'repair_seq': event['seq'], 'source_event_seqs': event.get('sourceEventSeqs', []),
        'source': '原生修复序号处的原始物理 B 日志；必须提供独立业务账本'}
        for event in audit['expected_repair_events'] if event['type'] == 'tool/result']
    require([row.get('native_call_id') for row in result.get('uncertainty', [])] == [row['native_call_id'] for row in uncertainty]
            and all(row.get('error') == source['native_error'] and row.get('repair_seq') == source['repair_seq']
                and row.get('source_event_seqs') == source['source_event_seqs']
                for row, source in zip(result['uncertainty'], uncertainty)), 'fault_dsh_native_uncertainty_hidden')
    evidence = {'schema': 'harness-lab-dsh-recovery-evidence/1', 'scope': scope, 'chain_id': pins['chain_id'],
        'original_goal_id': final_goal['id'], 'original_committed_bytes_preserved': True,
        'repair_events_verified_against_actual_original_physical_log': True,
        'uncertainty': uncertainty, 'original_open_tail_inbox_splices': audit['actual_inbox_splices_in_open_tail'],
        'native_goal_phase_at_end': final_goal['phase'], 'business_automatic_completion_implied': False,
        'recovery_granularity': '官方中断轮次修复后的原始持久化 Goal；不保证任意执行位置恢复'}
    private(paths.run / 'trusted/native-recovery-dsh-evidence.json', evidence)
    return evidence

def terminal_completion(paths, pins, scope, attempt):
    """与作用域无关的终态事实，不假定 I22 暂停或恢复。"""
    directory = paths.run / 'attempts' / attempt / 'candidate'
    result = read(directory / 'result.json')
    require(result.get('status') == result.get('native_status') == 'completed' and not result.get('error')
            and not result.get('error_message'), 'fault_native_completion_missing')
    terminal = detailed.native_terminal(replace(paths, run=directory.parent))
    require(terminal['normal_end'], 'fault_native_terminal_not_normal')
    if pins['candidate'] == 'deepagents_langgraph':
        state = read(directory / 'state-snapshot.json')
        require(not state.get('next') and not state.get('interrupts') and all(not row.get('error')
            and not row.get('interrupts') for row in state.get('tasks', [])), 'fault_native_checkpoint_unfinished')
    else:
        persisted = io.dsh_storage(paths, directory, scope)
        view = result.get('native_result', {}).get('faultOutcome', {})
        require(persisted['goal'].get('phase') == 'complete' and view.get('kind') == 'completed'
                and matches_live_view(view.get('goal'), persisted['goal'], 'disarmed'), 'fault_original_native_goal_not_complete')
        require(persisted['goal']['objective'] == read(paths.fixture / 'candidate-request.json')['prompt'], 'fault_goal_objective_changed')
        facts = read(directory / 'native-fault-terminal.json')
        require(facts.get('schema') == 'harness-lab-dsh-fault-terminal/1' and facts.get('scope') == scope
                and facts.get('chain_id') == pins['chain_id'] and facts.get('session_id') == scope['session_id']
                and facts.get('kind') == 'completed' and facts.get('native_errors') == 0
                and facts.get('concurrent_input') is False
                and matches_live_view(facts.get('goal'), persisted['goal'], 'disarmed')
                and facts.get('last_native_turn_end', {}).get('seq') == persisted['turn_end']['seq']
                and facts['last_native_turn_end'].get('data') == persisted['turn_end']['data']
                and facts.get('artifact', {}).get('physical_sha256') == persisted['artifact']['physical_sha256']
                and facts.get('business_recovery_verified_by_candidate') is False, 'fault_dsh_native_terminal_facts_invalid')
    return True


def grade(args, paths, scope, pins):
    """将失败保留在新判定中，不通过重跑掩盖。"""
    checks, axes = [], {}
    def check(name, callback):
        try:
            value = callback()
            checks.append({'name': name, 'passed': True})
            return value
        except Exception as error:
            checks.append({'name': name, 'passed': False, 'error': type(error).__name__ + ':' + str(error)[:160]})
            return None
    ledger = check('final_independent_ledger', lambda: ledger_request(paths, 'GET', '/admin/ledger', params={'experiment_id': 'fault-recovery', 'variant': args.chain_id}))
    if ledger is not None:
        private(paths.run / 'trusted/ledger-final.json', ledger)
    budget = check('cumulative_model_budget', lambda: model_snapshot(paths))
    if budget is not None:
        create(paths.run / 'model-calls.json', budget)
    events = check('complete_trusted_tool_audit', lambda: [row for _, _, row in io.jsonl(paths.run / 'tool-events.jsonl')])
    if (paths.run / 'trusted/crash-boundary.json').exists() and budget is not None and events is not None:
        def prefix_integrity():
            boundary = read(paths.run / 'trusted/crash-boundary.json')
            old = boundary['model_budget']
            require(old['token_sha256'] == budget['token_sha256'] and old['cap'] == budget['cap']
                    and budget['rows'][:old['count']] == old['rows'], 'fault_model_budget_reset_or_prefix_changed')
            audit = boundary['tool_audit']
            current = regular(paths.run / 'tool-events.jsonl')
            require(len(current) >= audit['bytes'] and sha(current[:audit['bytes']]) == audit['sha256'],
                    'fault_trusted_tool_history_rewritten_after_crash')
            return True
        prefix = check('original_budget_and_tool_prefix', prefix_integrity)
    else:
        prefix = True if args.fault != 'kill_before_write' else None
    operations = None
    if ledger is not None and events is not None:
        before = read(paths.run / 'trusted/ledger-before-recovery.json') if (paths.run / 'trusted/ledger-before-recovery.json').exists() else None
        operations = check('operation_sources', lambda: operation_analysis(ledger, events, scope, before))
        if operations is not None:
            create(paths.run / 'operation-analysis.json', {'operations': operations})
        task, _ = detailed.task_definition('tools')
        business = grade_tools(args.seed, scope, paths.workspace, ledger, events, task['workspace_inputs']['request.json'])
        create(paths.run / 'business-verdict.json', business)
        axes['task_result'] = {'passed': business['passed'], 'source': 'business-verdict.json'}
    else:
        business = None
        axes['task_result'] = {'passed': False, 'evidence_invalid': True}
    axes['injection_valid'] = bool(ledger is not None and budget is not None and check('injection_window', lambda: injection_valid(args, paths, scope, ledger, budget)))
    orchestration = check('host_lifecycle', lambda: read(paths.run / 'trusted/orchestration-final.json'))
    attempt = 'worker-b' if (paths.run / 'trusted/worker-b.json').exists() else 'worker-a'
    terminal = check('native_terminal', lambda: terminal_completion(paths, pins, scope, attempt))
    processes = check('exact_processes_controls_mounts', lambda: process_evidence(paths, scope, pins, args))
    def checked_resources():
        require(processes is not None, 'fault_process_evidence_not_validated')
        return resource_evidence(paths, scope, pins, processes)
    resources = check('resource_limits', checked_resources)
    if resources is not None:
        private(paths.run / 'trusted/resource-summary.json', resources)
    if args.fault == 'kill_before_write':
        verifier = deepagents_resume_evidence if args.candidate == 'deepagents_langgraph' else deepseek_resume_evidence
        recovered = check('original_native_crash_recovery', lambda: verifier(paths, scope, pins)) if attempt == 'worker-b' else None
    else:
        recovered = True
    process_ok = bool(orchestration and resources and orchestration.get('errors') == [] and orchestration.get('scope') == scope
        and orchestration.get('chain_id') == args.chain_id and orchestration.get('used_active_seconds', 301) <= 300
        and orchestration.get('timed_out') is False and resources.get('passed') is True)
    axes['budget_and_process'] = {'passed': bool(process_ok and prefix and budget is not None and not budget['in_flight']),
        'model_calls': budget['count'] if budget else None, 'active_limit_seconds': 300, 'model_call_cap': 20}
    axes['replay_behavior'] = {'operations_file': 'operation-analysis.json' if operations is not None else None,
        'server_idempotency_is_candidate_no_replay': False,
        'reissued_after_commit': sum(len(row['reissued_after_commit_request_ids']) for row in operations or [])}
    duplicate = any(row['actual_duplicate'] for row in operations or [])
    source_valid = operations is not None and ledger is not None and budget is not None and prefix is not False
    if duplicate and source_valid:
        handling = 'unsafe_duplicate'
    elif not axes['injection_valid'] or not source_valid:
        handling = 'evidence_invalid'
    elif business and business['passed'] and terminal and recovered and axes['budget_and_process']['passed']:
        handling = 'automatic_completed'
    else:
        # 显式失败或待确认必须使用已审查的原生证据
        # 解码器。仅凭产物缺失、预算耗尽或候选自由文本，
        # 不能认定已如实进入待确认状态。
        handling = 'incomplete'
    axes['handling_outcome'] = handling
    verdict = {'schema': 'harness-lab-fault-verdict/1', 'candidate': args.candidate, 'chain_id': args.chain_id,
        'seed': args.seed, 'fault': args.fault, 'write_mode': args.write_mode, 'axes': axes, 'checks': checks,
        'passed': handling == 'automatic_completed', 'implementation_stage': 'docker_chain_evaluated',
        'limitations': ['只有此链具有已验证恢复证据时才声称原生恢复；校准单独记录',
                        '原生显式失败/待确认解码器尚未实现；不根据自由文本推断',
                        'DSH 在原生修复后恢复原始持久化 Goal，不保证恢复任意中断指令；格式错误的完整尾行一律拒绝',
                        '不保证恰好一次；仅覆盖一条本地 Docker 链']}
    create(paths.run / 'verdict.json', verdict)
    return verdict['passed']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=ACTIONS)
    parser.add_argument('--candidate', choices=CANDIDATES, required=True)
    parser.add_argument('--chain-id', type=identifier, required=True)
    parser.add_argument('--seed', type=int, choices=(101, 202, 303, 404, 505), required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--fault', choices=FAULTS, required=True)
    parser.add_argument('--write-mode', choices=('idempotent', 'non_idempotent'), required=True)
    args = parser.parse_args()
    require(environment()['model_credentials_injected'] is False, 'fault_bench_must_not_receive_remote_model_configuration')
    config, paths, scope = context(args)
    if args.action == 'prepare':
        prepare(args, config, paths, scope)
        return 0
    pins = pins_for(args, paths, scope)
    if args.action == 'configure':
        configure(args, paths, scope)
        return 0
    if args.action == 'bind-start':
        return 0 if bind_start(args, paths, scope, pins) else 3
    if args.action == 'observe-gate':
        # 健康只读轮询以 0 退出；独占可信信号文件存在
        # 表示命中。错误仍判失败，不能算未命中。
        print(json.dumps({'gate_hit': observe_gate(args, paths, scope)}))
        return 0
    if args.action == 'after-kill':
        return 0 if after_kill(args, paths, scope, pins) else 3
    if args.action == 'bind-crash-resume':
        return 0 if bind_crash_resume(args, paths, scope, pins) else 3
    return 0 if grade(args, paths, scope, pins) else 1


if __name__ == '__main__':
    raise SystemExit(main())
