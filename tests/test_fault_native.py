"""I23 合成故障边界校准，不声称真实 Agent 恢复。

下方 SQLite 与 WAL 为真实数据库测试数据。公开原生 API
仅在绑定拒绝测试中模拟。通过后仍需独立候选探针与真实故障链。
"""
from __future__ import annotations

import asyncio
from collections import namedtuple
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import sys
from types import SimpleNamespace

import ormsgpack
import pytest

from runner import fault_recovery as f


SCOPE = {'experiment_id': 'fault-recovery', 'variant': 'crash-calibration', 'user_id': 'alice', 'session_id': 'main'}
PROMPT = 'Original full objective. No replacement or continue input.'
ADAPTER = Path(__file__).resolve().parents[1] / 'adapters/deepagents_langgraph'


@pytest.fixture
def crash(monkeypatch):
    spec = importlib.util.spec_from_file_location('_i23_crash_helper', ADAPTER / 'fault_recovery.py')
    module = importlib.util.module_from_spec(spec)
    monkeypatch.syspath_prepend(str(ADAPTER))
    spec.loader.exec_module(module)
    return module


def human(text=PROMPT):
    return ormsgpack.Ext(5, ormsgpack.packb(['langchain_core.messages.human', 'HumanMessage',
        {'content': text, 'type': 'human', 'id': 'original-user-message'}, 'model_validate_json']))


@pytest.fixture
def natural(tmp_path):
    state, workspace = tmp_path / 'state', tmp_path / 'workspace'
    state.mkdir()
    workspace.mkdir()
    database = state / 'checkpoints.sqlite'
    connection = sqlite3.connect(database)
    connection.execute('PRAGMA journal_mode=WAL')
    connection.executescript('''CREATE TABLE checkpoints (
        thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '', checkpoint_id TEXT NOT NULL,
        parent_checkpoint_id TEXT, type TEXT, checkpoint BLOB, metadata BLOB,
        PRIMARY KEY(thread_id,checkpoint_ns,checkpoint_id));
        CREATE TABLE writes (thread_id TEXT NOT NULL, checkpoint_ns TEXT NOT NULL DEFAULT '',
        checkpoint_id TEXT NOT NULL, task_id TEXT NOT NULL, task_path TEXT NOT NULL DEFAULT '',
        idx INTEGER NOT NULL, channel TEXT NOT NULL, type TEXT, value BLOB,
        PRIMARY KEY(thread_id,checkpoint_ns,checkpoint_id,task_id,idx));''')
    ai = ormsgpack.Ext(5, ormsgpack.packb(['langchain_core.messages.ai', 'AIMessage', {'type': 'ai',
        'tool_calls': [{'id': 'native-crash-call', 'name': 'lab_write',
            'args': {'operation_id': 'op-crash', 'item_id': 'item-crash', 'quantity': 2}}]}, 'model_validate_json']))
    checkpoint = {'id': 'head-a', 'channel_values': {'messages': [human(), ai]}, 'channel_versions': {}, 'versions_seen': {}}
    encoded = ormsgpack.packb(checkpoint)
    connection.execute("INSERT INTO checkpoints VALUES('main','','head-a',NULL,'msgpack',?,?)", (encoded, b'{}'))
    connection.execute("INSERT INTO writes VALUES('main','','head-a','finished-tool','tools',0,'messages','msgpack',?)",
        (ormsgpack.packb({'tool_call_id': 'call-success', 'status': 'success'}),))
    connection.commit()
    yield SimpleNamespace(state=state, workspace=workspace, db=connection, blob=encoded)
    connection.close()


def test_wal_committed_head_and_pending_writes_are_read_without_changing_original(natural):
    # 保持已提交的测试连接打开，使 WAL 保持非空。
    assert (natural.state / 'checkpoints.sqlite-wal').stat().st_size > 0
    original = f.io.inventory(natural.state, natural.workspace)
    audited = f.natural_sqlite_audit(natural, SCOPE, {'prompt': PROMPT})
    assert audited['checkpoint_id'] == 'head-a'
    assert audited['checkpoint_blob_sha256'] == f.sha(natural.blob)
    assert audited['pending_write_keys'] == [['finished-tool', 'messages']]
    assert audited['pending_writes'][0]['value']['tool_call_id'] == 'call-success'
    assert audited['native_inventory'] == original == f.io.inventory(natural.state, natural.workspace)
    assert audited['pause_index_used'] is False and audited['original_files_unchanged'] is True


def test_older_base_database_cannot_hide_the_committed_wal_head(natural):
    newer = ormsgpack.packb({'id': 'head-z', 'channel_values': {'messages': [human()]}})
    natural.db.execute("INSERT INTO checkpoints VALUES('main','','head-z','head-a','msgpack',?,?)", (newer, b'{}'))
    natural.db.commit()
    audited = f.natural_sqlite_audit(natural, SCOPE, {'prompt': PROMPT})
    assert audited['checkpoint_id'] == 'head-z'
    assert len(audited['checkpoint_history']) == 2
    assert audited['pending_writes'] == []


@pytest.mark.parametrize('kind', ['prompt', 'thread', 'namespace', 'pause_index', 'journal', 'symlink'])
def test_foreign_repaired_or_paused_storage_cannot_become_crash_durability(natural, kind):
    if kind == 'prompt':
        with pytest.raises(ValueError, match='original_user_objective'):
            f.natural_sqlite_audit(natural, SCOPE, {'prompt': 'new objective'})
        return
    if kind == 'thread':
        natural.db.execute("UPDATE checkpoints SET thread_id='someone-else'")
    elif kind == 'namespace':
        natural.db.execute("UPDATE checkpoints SET checkpoint_ns='foreign'")
    elif kind == 'pause_index':
        (natural.state / 'pause-index.json').write_text('{}')
    elif kind == 'journal':
        (natural.state / 'checkpoints.sqlite-journal').write_bytes(b'hot-journal')
    else:
        (natural.state / 'linked').symlink_to(natural.workspace)
    natural.db.commit()
    with pytest.raises(ValueError):
        f.natural_sqlite_audit(natural, SCOPE, {'prompt': PROMPT})


def test_unknown_msgpack_extension_is_opaque_data_not_a_python_constructor():
    hostile = ormsgpack.packb({'value': ormsgpack.Ext(5, ormsgpack.packb([
        'subprocess', 'Popen', {'args': ['forbidden']}, 'model_validate_json']))})
    assert f.decode_checkpoint(hostile)['value']['opaque_extension'] == 5


def request(mode='new'):
    result = {'run_id': SCOPE['variant'], 'session_id': 'main',
        'candidate_options': {'case': 'tools', 'trusted_scope_file': '/config/scope.json'},
        'fault_recovery': {'mode': mode, 'chain_id': SCOPE['variant'],
            'attempt_id': 'worker-a' if mode == 'new' else 'worker-b', 'binding_file': '/config/native-crash.json'}}
    if mode == 'new':
        result['prompt'] = PROMPT
    return result


@pytest.mark.parametrize('change', ['prompt', 'followup', 'lifecycle', 'foreign_chain', 'foreign_attempt', 'pause_path'])
def test_crash_request_rejects_new_input_or_i22_reference(crash, change):
    value = request('crash-resume')
    if change in {'prompt', 'followup', 'lifecycle'}:
        value[change] = 'forbidden'
    elif change == 'foreign_chain':
        value['fault_recovery']['chain_id'] = 'other-chain'
    elif change == 'foreign_attempt':
        value['fault_recovery']['attempt_id'] = 'worker-a'
    else:
        value['fault_recovery']['binding_file'] = '/config/lifecycle.json'
    with pytest.raises(ValueError):
        crash.validate_request(value)


Snapshot = namedtuple('Snapshot', 'config next tasks interrupts values')
Tuple = namedtuple('Tuple', 'config pending_writes checkpoint')
Task = namedtuple('Task', 'error interrupts')


def public_resume(crash, *, head='head-a', keys=None, unfinished=True):
    config = {'configurable': {'thread_id': 'main', 'checkpoint_ns': '', 'checkpoint_id': head}}
    pending = [('finished-tool', 'messages', {'tool_call_id': 'call-success'})] if keys is None else keys
    checkpoint = Tuple(config, pending, {'id': head})
    snapshot = Snapshot(config, ('tools',) if unfinished else (), (Task(None, ()),), (), {})
    class Graph:
        async def aget_state(self, supplied):
            assert 'checkpoint_id' not in supplied['configurable']
            return snapshot
    class Saver:
        async def aget_tuple(self, supplied):
            assert supplied['configurable'] == {'thread_id': 'main', 'checkpoint_ns': ''}
            return checkpoint
    helper = object.__new__(crash.CrashRecovery)
    helper.request, helper.scope = request('crash-resume'), SCOPE
    helper.binding = {'chain_id': SCOPE['variant'], 'recovery': {'checkpoint_id': 'head-a',
        'pending_write_keys': [['finished-tool', 'messages']], 'boundary_sha256': 'b' * 64, 'audit_sha256': 'a' * 64}}
    return helper, Graph(), Saver()


def test_public_resume_uses_native_head_and_keeps_pending_writes(crash):
    helper, graph, saver = public_resume(crash)
    value = asyncio.run(helper.check_resume(graph, saver))
    assert value['input'] is None and value['new_prompt_submitted'] is False
    assert value['pause_index_used'] is False
    assert value['pending_write_keys'] == [['finished-tool', 'messages']]
    assert value['snapshot_before_resume']['next'] == ('tools',)


def test_native_send_projection_matches_physical_constructor_without_changing_input(crash, monkeypatch):
    class Send:
        __module__ = 'langgraph.types'
        def __init__(self, node, arg, timeout=None):
            self.node, self.arg, self.timeout = node, arg, timeout
    monkeypatch.setitem(sys.modules, 'langgraph.types', SimpleNamespace(Send=Send))
    arguments = {'tool_call': {'id': 'native-call', 'name': 'lab_write'}, 'state': {'messages': []}}
    send = Send('tools', arguments)
    original = {'pending': [('native-task', '__pregel_tasks', send)]}
    projected = crash.crash_projection(original)
    payload = ormsgpack.packb(['langgraph.types', 'Send', ['tools', arguments]])
    physical = f.decode_checkpoint(ormsgpack.packb(ormsgpack.Ext(1, payload)))
    assert projected['pending'][0][2] == f.native_json_projection(physical)
    assert original['pending'][0][2] is send and send.arg is arguments
    assert projected['pending'][0][2]['native_send']['arg'] == arguments
    assert projected['pending'][0][2]['native_send']['arg'] is not arguments
    with pytest.raises(TypeError, match='不支持的 I23 原生 Send 结构'):
        crash.crash_projection(Send('tools', arguments, timeout=5))


@pytest.mark.parametrize('record', [
    ['other.module', 'Send', ['tools', {}]],
    ['langgraph.types', 'Send', ['', {}]],
    ['langgraph.types', 'Send', ['tools', {}, 5]],
    ['langgraph.types', 'Send', {'node': 'tools', 'arg': {}}],
])
def test_unknown_or_nonfixed_send_constructor_stays_opaque(record):
    value = f.decode_checkpoint(ormsgpack.packb(ormsgpack.Ext(1, ormsgpack.packb(record))))
    assert value['opaque_extension'] == 1
    with pytest.raises(ValueError, match='unsupported_native_public_projection'):
        f.native_json_projection(value)


@pytest.mark.parametrize('flag', [False, True])
def test_live_pending_task_uses_pinned_push_boolean_path_marker(flag):
    message = {'native_class': 'langchain_core.messages.tool.ToolMessage',
        'fields': {'type': 'tool', 'id': 'tool-result', 'content': 'saved result'}}
    path = ['__pregel_push', ['__pregel_pull', 'model'], 0, flag]
    audit = {'messages': [], 'pending_writes': [{'task_id': 'finished-task', 'channel': 'messages',
        'type': 'msgpack', 'task_path': '~__pregel_push, ~__pregel_pull, model, 0000000000, ' + f'{flag:010d}',
        'value': [message]}]}
    snapshot = {'tasks': [{'id': 'finished-task', 'path': path, 'error': None, 'interrupts': [],
        'result': {'messages': [message]}}]}
    assert f.live_message_projection(audit, snapshot) == [message]
    audit['pending_writes'][0]['task_path'] += '-foreign'
    with pytest.raises(ValueError, match='pending_message_not_current_finished_task'):
        f.live_message_projection(audit, snapshot)


@pytest.mark.parametrize('change', ['head', 'pending', 'completed'])
def test_public_resume_refuses_foreign_head_lost_pending_or_finished_task(crash, change):
    helper, graph, saver = public_resume(crash,
        head='other-head' if change == 'head' else 'head-a',
        keys=[] if change == 'pending' else None, unfinished=change != 'completed')
    with pytest.raises(ValueError):
        asyncio.run(helper.check_resume(graph, saver))


def test_b_binding_uses_only_original_natural_state_and_remaining_budget(natural, tmp_path, monkeypatch):
    natural.fixture, natural.run, natural.budget = tmp_path / 'fixture', tmp_path / 'run', tmp_path / 'budget'
    for directory in (natural.fixture / 'attempts/worker-b', natural.run / 'trusted', natural.run / 'attempts/worker-b/candidate', natural.budget):
        directory.mkdir(parents=True)
    f.create(natural.fixture / 'candidate-request.json', {**request(), 'timeout_seconds': 300})
    f.create(natural.fixture / 'scope.json', SCOPE)
    (natural.fixture / 'scope.token').write_text('local-tool-token')
    (natural.budget / 'run.token').write_text('same-counted-model-token')
    f.create(natural.run / 'trusted/pins.json', {})
    f.create(natural.run / 'trusted/gate-hit.json', {'events': [{'phase': 'prewrite_hit', 'event_id': 3,
        'native_call_id': 'native-crash-call', 'bridge_call_id': 'bridge-crash-call'}]})
    f.create(natural.run / 'trusted/crash-boundary.json', {'scope': SCOPE})
    boundary_hash = f.sha(f.regular(natural.run / 'trusted/crash-boundary.json'))
    f.create(natural.run / 'trusted/resume-clock.json', {'chain_id': SCOPE['variant'], 'scope': SCOPE,
        'boundary_sha256': boundary_hash, 'used_active_seconds': 42.6})
    monkeypatch.setattr(f.io, 'da_runtime', lambda *args: 'd' * 64)
    monkeypatch.setattr(f, 'expected_target', lambda *args: {'operation_id': 'op-crash', 'item_id': 'item-crash', 'quantity': 2})
    args = SimpleNamespace(candidate='deepagents_langgraph', chain_id=SCOPE['variant'])
    pins = {'candidate': args.candidate, 'chain_id': args.chain_id, 'candidate_image_id': 'sha256:' + 'a' * 64,
        'source_export_sha256': 'b' * 64, 'adapter_export_sha256': 'c' * 64}
    before = f.io.inventory(natural.state, natural.workspace)
    f.deepagents_binding(args, natural, SCOPE, pins, {'natural_native_inventory': before})
    restored = f.read(natural.fixture / 'attempts/worker-b/request.json')
    trusted = f.read(natural.fixture / 'attempts/worker-b/native-crash.json')
    assert 'prompt' not in restored and 'followup' not in restored
    assert restored['timeout_seconds'] == 257 and trusted['budget']['used_active_seconds'] == 42.6
    assert trusted['recovery']['checkpoint_id'] == 'head-a'
    assert trusted['recovery']['pending_write_keys'] == [['finished-tool', 'messages']]
    assert before == f.io.inventory(natural.state, natural.workspace)
    assert not (natural.state / 'pause-index.json').exists()


def native_message(kind, message_id, content='', **fields):
    module, name = {'human': ('human', 'HumanMessage'), 'ai': ('ai', 'AIMessage'),
        'tool': ('tool', 'ToolMessage'), 'remove': ('modifier', 'RemoveMessage')}[kind]
    return ormsgpack.Ext(5, ormsgpack.packb(['langchain_core.messages.' + module, name,
        {'type': kind, 'id': message_id, 'content': content, **fields}, 'model_validate_json']))


@pytest.fixture
def delta(natural):
    """物理增量存储：父子顺序不同于 ID 顺序，并带有分支。

    与固定 Deep Agents 冒烟的六条真实 checkpoint 相同，最新
    checkpoint 不含 messages 字段。不构造 SDK、模型或执行状态。
    """
    natural.db.execute('DELETE FROM writes')
    natural.db.execute('DELETE FROM checkpoints')
    def checkpoint(cid, parent, messages=None, *, present=False):
        value = {'id': cid, 'channel_values': {'messages': messages} if present else {},
            'channel_versions': {'messages': '1'}, 'versions_seen': {}}
        natural.db.execute('INSERT INTO checkpoints VALUES(?,?,?,?,?,?,?)',
            ('main', '', cid, parent, 'msgpack', ormsgpack.packb(value), b'{}'))
    def write(cid, task, path, value, *, index=0, channel='messages', kind='msgpack'):
        blob = ormsgpack.packb(value) if kind == 'msgpack' else value
        natural.db.execute('INSERT INTO writes VALUES(?,?,?,?,?,?,?,?,?)',
            ('main', '', cid, task, path, index, channel, kind, blob))
    checkpoint('80-root', None)
    checkpoint('20-parent', '80-root')
    checkpoint('90-unrelated-fork', '80-root', [human('unrelated branch')], present=True)
    checkpoint('99-head', '20-parent')
    write('80-root', 'input', '~__pregel_pull, __start__', [human()])
    ai = native_message('ai', 'model-before-kill', tool_calls=[{'id': 'native-crash-call',
        'name': 'lab_write', 'args': {'operation_id': 'op-crash'}}])
    write('20-parent', 'model-task', '~__pregel_pull, model', [ai])
    pending = native_message('tool', 'completed-sibling-output', 'receipt', tool_call_id='sibling-call')
    write('99-head', 'completed-sibling', '~__pregel_push, 0000000001', [pending])
    write('99-head', 'empty-output-task', '~__pregel_push, 0000000002', b'',
        channel='__no_writes__', kind='null')
    natural.db.commit()
    natural.checkpoint, natural.write = checkpoint, write
    return natural


def message_ids(messages):
    return [message['fields'].get('id') for message in messages]


def test_delta_parent_writes_restore_original_messages_without_target_pending_or_foreign_fork(delta):
    before = f.io.inventory(delta.state, delta.workspace)
    audit = f.natural_sqlite_audit(delta, SCOPE, {'prompt': PROMPT})
    assert 'messages' not in audit['checkpoint']['channel_values']
    assert message_ids(audit['messages']) == ['original-user-message', 'model-before-kill']
    history = audit['message_history']
    assert history['ancestor_checkpoint_ids'] == ['80-root', '20-parent']
    assert history['seed_checkpoint_id'] is None and history['target_pending_writes_excluded'] is True
    assert [step['checkpoint_id'] for step in history['steps']] == ['80-root', '20-parent']
    assert history['reconstructed_messages_sha256'] == f.digest(audit['messages'])
    assert audit['pending_write_keys'] == [['completed-sibling', 'messages'], ['empty-output-task', '__no_writes__']]
    assert audit['pending_writes'][1]['value'] is None
    assert before == audit['native_inventory'] == f.io.inventory(delta.state, delta.workspace)


def test_delta_history_uses_official_task_path_task_id_and_index_order(delta):
    delta.db.execute("UPDATE writes SET task_path='path-2' WHERE checkpoint_id='20-parent'")
    delta.write('20-parent', 'alpha', 'path-1', [native_message('ai', 'model-before-kill', 'alpha-zero')])
    delta.write('20-parent', 'beta', 'path-1', [native_message('ai', 'model-before-kill', 'beta-one')], index=1)
    delta.write('20-parent', 'alpha', 'path-1', [native_message('ai', 'model-before-kill', 'alpha-one')], index=1)
    delta.db.commit()
    audit = f.natural_sqlite_audit(delta, SCOPE, {'prompt': PROMPT})
    writes = audit['message_history']['steps'][-1]['writes']
    assert [(row['task_path'], row['task_id'], row['index']) for row in writes] == [
        ('path-1', 'alpha', 0), ('path-1', 'alpha', 1), ('path-1', 'beta', 1), ('path-2', 'model-task', 0)]
    assert len(audit['messages']) == 2 and audit['messages'][-1]['fields']['content'] == ''


@pytest.mark.parametrize('storage', ['delta_head', 'delta_ancestor', 'plain_ancestor'])
def test_delta_snapshot_seed_and_seed_step_writes_are_kept_separate_from_target_pending(delta, storage):
    target = '99-head' if storage == 'delta_head' else '20-parent'
    seed = [human()]
    if storage != 'plain_ancestor':
        seed = ormsgpack.Ext(7, ormsgpack.packb(seed))
    parent = '20-parent' if target == '99-head' else '80-root'
    delta.db.execute('DELETE FROM checkpoints WHERE checkpoint_id=?', (target,))
    delta.checkpoint(target, parent, seed, present=True)
    delta.db.commit()
    audit = f.natural_sqlite_audit(delta, SCOPE, {'prompt': PROMPT})
    expected = ['original-user-message'] if storage == 'delta_head' else ['original-user-message', 'model-before-kill']
    assert message_ids(audit['messages']) == expected
    assert audit['message_history']['seed_checkpoint_id'] == target
    if storage == 'delta_head':
        stored = audit['checkpoint']['channel_values']['messages']
        assert set(stored) == {'native_delta_snapshot'}
        assert f.native_json_projection(stored) == [f.native_json_projection(audit['messages'])]
        assert audit['message_history']['steps'] == []
    else:
        assert audit['message_history']['ancestor_checkpoint_ids'] == ['20-parent']


@pytest.mark.parametrize('change', ['missing_parent', 'cycle', 'missing_version', 'opaque_message',
    'message_encoding', 'wrong_original', 'missing_original'])
def test_delta_reconstruction_rejects_missing_corrupt_or_unbound_durable_material(delta, change):
    if change == 'missing_parent':
        delta.db.execute("UPDATE checkpoints SET parent_checkpoint_id='absent' WHERE checkpoint_id='20-parent'")
    elif change == 'cycle':
        delta.db.execute("UPDATE checkpoints SET parent_checkpoint_id='99-head' WHERE checkpoint_id='20-parent'")
    elif change == 'missing_version':
        blob = ormsgpack.packb({'id': '99-head', 'channel_values': {}, 'channel_versions': {}})
        delta.db.execute("UPDATE checkpoints SET checkpoint=? WHERE checkpoint_id='99-head'", (blob,))
    elif change == 'opaque_message':
        blob = ormsgpack.packb([ormsgpack.Ext(5, ormsgpack.packb(['subprocess', 'Popen', {}, 'model_validate_json']))])
        delta.db.execute("UPDATE writes SET value=? WHERE checkpoint_id='80-root'", (blob,))
    elif change == 'message_encoding':
        delta.db.execute("UPDATE writes SET type='pickle' WHERE checkpoint_id='80-root'")
    elif change == 'missing_original':
        delta.db.execute("DELETE FROM writes WHERE checkpoint_id='80-root'")
    delta.db.commit()
    with pytest.raises(ValueError):
        f.natural_sqlite_audit(delta, SCOPE, {'prompt': 'replacement objective' if change == 'wrong_original' else PROMPT})


def projected_message(kind, key, content=''):
    return f.decode_checkpoint(ormsgpack.packb(native_message(kind, key, content)))


def test_plain_message_reducer_matches_id_replace_delete_remove_all_and_last_overwrite():
    original = projected_message('human', 'human', 'objective')
    first = projected_message('ai', 'same', 'first')
    replacement = projected_message('ai', 'same', 'replacement')
    deleted = projected_message('remove', 'same')
    missing = projected_message('remove', 'never-existed')
    reset = projected_message('remove', '__remove_all__')
    last = projected_message('ai', 'last', 'final')
    assert f.replay_message_writes([original], [[first], [replacement]]) == [original, replacement]
    assert f.replay_message_writes([original], [[first, deleted, missing]]) == [original]
    assert f.replay_message_writes([original], [[first, reset, last]]) == [last]
    typed = ormsgpack.Ext(2, ormsgpack.packb(['langgraph.types', 'Overwrite', {'value': [native_message('ai', 'last', 'final')],
        'type': '__overwrite__'}]))
    overwrite = f.decode_checkpoint(ormsgpack.packb(typed))
    assert f.replay_message_writes([original], [[first], {'__overwrite__': [replacement]}, overwrite, [original]]) == [last, original]
    assert f.replay_message_writes([original], [[first], overwrite, [replacement]], live=True) == [last]
    with pytest.raises(ValueError, match='multiple_native_message_overwrites'):
        f.replay_message_writes([original], [overwrite, overwrite], live=True)
    with pytest.raises(ValueError, match='unsupported_native_message'):
        f.replay_message_writes([original], ['unreviewed coercion'])


def live_snapshot(audit):
    task = ['completed-sibling', 'tools', ['__pregel_push', 1], None, [], None,
        {'messages': [f.native_json_projection(audit['pending_writes'][0]['value'][0])]}]
    snapshot = {'tasks': [task], 'values': {}}
    snapshot['values']['messages'] = f.native_json_projection(f.live_message_projection(audit, snapshot))
    return snapshot


def test_current_head_live_snapshot_includes_only_bound_completed_pending_messages(delta):
    audit = f.natural_sqlite_audit(delta, SCOPE, {'prompt': PROMPT})
    snapshot = live_snapshot(audit)
    assert message_ids(f.live_message_projection(audit, snapshot)) == [
        'original-user-message', 'model-before-kill', 'completed-sibling-output']
    assert message_ids(audit['messages']) == ['original-user-message', 'model-before-kill']
    for field, value in ((0, 'foreign-task'), (2, ['__pregel_push', 8]), (3, 'error'), (6, None)):
        changed = deepcopy(snapshot)
        changed['tasks'][0][field] = value
        with pytest.raises(ValueError):
            f.live_message_projection(audit, changed)


def test_live_null_input_is_applied_before_completed_task_outputs_without_entering_base(delta):
    audit = f.natural_sqlite_audit(delta, SCOPE, {'prompt': PROMPT})
    null_message = projected_message('human', 'null-input', 'new native input')
    audit['pending_writes'].append({'task_id': '00000000-0000-0000-0000-000000000000',
        'task_path': '', 'index': 0, 'channel': 'messages', 'type': 'msgpack', 'value': [null_message]})
    snapshot = live_snapshot(audit)
    assert message_ids(f.live_message_projection(audit, snapshot)) == [
        'original-user-message', 'model-before-kill', 'null-input', 'completed-sibling-output']
    assert message_ids(audit['messages']) == ['original-user-message', 'model-before-kill']


def resume_record(delta, audit):
    delta.fixture, delta.run = delta.state.parent / 'fixture', delta.state.parent / 'run'
    directory = delta.run / 'attempts/worker-b/candidate'
    for folder in (delta.fixture / 'attempts/worker-b', delta.run / 'trusted', directory):
        folder.mkdir(parents=True)
    f.create(delta.run / 'trusted/native-crash-sqlite.json', audit)
    f.create(delta.run / 'trusted/crash-boundary.json', {'natural_native_inventory': audit['native_inventory']})
    audit_sha = f.sha(f.regular(delta.run / 'trusted/native-crash-sqlite.json'))
    f.create(delta.fixture / 'attempts/worker-b/native-crash.json', {'recovery': {'audit_sha256': audit_sha}})
    config = {'configurable': {'thread_id': 'main', 'checkpoint_ns': '', 'checkpoint_id': '99-head'}}
    snapshot = live_snapshot(audit)
    snapshot['config'] = config
    result = {'schema': 'harness-lab-native-crash-resume/1', 'scope': SCOPE, 'chain_id': SCOPE['variant'],
        'attempt_id': 'worker-b', 'physical_audit_sha256': audit_sha,
        'boundary_sha256': f.sha(f.regular(delta.run / 'trusted/crash-boundary.json')),
        'new_prompt_submitted': False, 'pause_index_used': False, 'input': None,
        'checkpoint_tuple': {'config': config, 'checkpoint': f.native_json_projection(audit['checkpoint']),
            'pending_writes': [[row['task_id'], row['channel'], f.native_json_projection(row['value'])]
                for row in audit['pending_writes']]},
        'snapshot_before_resume': snapshot, 'pending_write_keys': audit['pending_write_keys'],
        'config': {'configurable': {'thread_id': 'main', 'checkpoint_ns': ''}, 'recursion_limit': 64}}
    path = directory / 'native-crash-resume.json'
    f.create(path, result)
    return path, result


def test_public_raw_saver_and_live_graph_are_verified_against_different_physical_projections(delta):
    audit = f.natural_sqlite_audit(delta, SCOPE, {'prompt': PROMPT})
    _, result = resume_record(delta, audit)
    assert 'messages' not in result['checkpoint_tuple']['checkpoint']['channel_values']
    assert len(result['snapshot_before_resume']['values']['messages']) == 3
    assert f.deepagents_resume_evidence(delta, SCOPE, {'chain_id': SCOPE['variant']}) is True


@pytest.mark.parametrize('change', ['explicit_null', 'manufactured_raw_messages', 'lost_pending_live',
    'forged_pending_value', 'foreign_pending_task', 'foreign_task_path', 'changed_versions'])
def test_public_crash_resume_rejects_raw_delta_or_pending_boundary_substitution(delta, change):
    audit = f.natural_sqlite_audit(delta, SCOPE, {'prompt': PROMPT})
    path, result = resume_record(delta, audit)
    if change == 'explicit_null':
        result['checkpoint_tuple']['checkpoint']['channel_values']['messages'] = None
    elif change == 'manufactured_raw_messages':
        result['checkpoint_tuple']['checkpoint']['channel_values']['messages'] = f.native_json_projection(audit['messages'])
    elif change == 'lost_pending_live':
        result['snapshot_before_resume']['values']['messages'] = f.native_json_projection(audit['messages'])
    elif change == 'forged_pending_value':
        result['checkpoint_tuple']['pending_writes'][0][2] = []
    elif change == 'foreign_pending_task':
        result['snapshot_before_resume']['tasks'][0][0] = 'not-the-completed-sibling'
    elif change == 'foreign_task_path':
        result['snapshot_before_resume']['tasks'][0][2] = ['__pregel_push', 9]
    else:
        result['checkpoint_tuple']['checkpoint']['channel_versions']['messages'] = 'replacement-version'
    path.write_text(json.dumps(result) + '\n')
    with pytest.raises(ValueError):
        f.deepagents_resume_evidence(delta, SCOPE, {'chain_id': SCOPE['variant']})
