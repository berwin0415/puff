"""Cost enrollment rejects active/foreign evidence without requiring task success."""
from copy import deepcopy
import hashlib
import json

import pytest

from scripts.summarize_research_usage import I24_CLOSURE, I24_SCOPE_CLOSURE, I25_CLOSURE, closure


def enrollment(tmp_path, *, change=None):
    run_id = 'closed-fault'
    relative = 'runs/2026-10-20/deepagents_langgraph/fault-recovery/' + run_id
    run = tmp_path / relative
    run.mkdir(parents=True)
    scope = {'experiment_id': 'fault-recovery', 'variant': run_id, 'user_id': 'alice', 'session_id': 'main'}
    documents = {
        'chain.json': {'schema': 'harness-lab-fault-chain/1', 'candidate': 'deepagents_langgraph', 'chain_id': run_id, 'scope': scope},
        'orchestration.json': {'schema': 'harness-lab-fault-orchestration/1', 'chain_id': run_id, 'scope': dict(scope),
            'finished_at': '2026-10-20T00:00:00Z', 'errors': ['implementation failed']},
        'cleanup.json': {'remaining': [], 'finished_at': '2026-10-20T00:00:01Z'},
        'verdict.json': {'passed': False},
    }
    if change == 'active':
        del documents['orchestration.json']['finished_at']
    elif change == 'foreign':
        documents['orchestration.json']['scope']['user_id'] = 'other'
    elif change == 'not-clean':
        documents['cleanup.json']['remaining'] = ['worker-id']
    entry = {'run_path': relative, 'run_id': run_id, 'candidate': 'deepagents_langgraph', 'case': 'fault-recovery',
        'budget_group': 'fault-recovery', 'budget_path': f'budgets/fault-recovery/{run_id}/{run_id}/calls.sqlite',
        'closure_kind': 'finished-fault-chain-and-cleanup', 'upstream_kind': 'remote', 'closure_sources': []}
    for name, document in documents.items():
        data = (json.dumps(document) + '\n').encode()
        (run / name).write_bytes(data)
        entry['closure_sources'].append({'path': relative + '/' + name, 'sha256': hashlib.sha256(data).hexdigest()})
    if change == 'other-budget':
        entry['budget_path'] = 'budgets/fault-recovery/other/other/calls.sqlite'
    elif change == 'modified':
        (run / 'cleanup.json').write_text('{}')
    return entry


def test_ended_failed_fault_run_is_spend_without_claiming_success(tmp_path):
    facts = closure(tmp_path, enrollment(tmp_path))
    assert facts['original_verdict_passed'] is False
    assert facts['error_count'] == 1 and facts['cleanup_independently_proven'] is True


@pytest.mark.parametrize('change', ['active', 'foreign', 'not-clean', 'other-budget', 'modified'])
def test_active_foreign_or_modified_fault_budget_is_not_enrolled(tmp_path, change):
    with pytest.raises(ValueError):
        closure(tmp_path, enrollment(tmp_path, change=change))


def freeze(tmp_path, entry, documents):
    run = tmp_path / entry['run_path']
    run.mkdir(parents=True)
    entry['closure_sources'] = []
    for name, document in documents.items():
        path = run / name
        path.parent.mkdir(parents=True, exist_ok=True)
        data = (json.dumps(document) + '\n').encode()
        path.write_bytes(data)
        entry['closure_sources'].append({'path': entry['run_path'] + '/' + name,
                                         'sha256': hashlib.sha256(data).hexdigest()})
    return entry


def stopped_container(identity, role, name, *, trial=None, image='services', exit_code=137):
    value = {'id': identity, 'role': role, 'image_id': image, 'name': '/' + name, 'mounts': [],
             'state': {'Status': 'exited', 'Running': False, 'Restarting': False, 'Pid': 0,
                       'ExitCode': exit_code, 'FinishedAt': '2026-10-21T00:00:00Z'}}
    if trial is not None:
        value['trial_id'] = trial
    return value


def i24_enrollment(tmp_path, change=None):
    trial = 'closed-i24'
    variant = trial + '-main-a'
    relative = 'runs/2026-10-21/deepagents_langgraph/scope-baseline/' + trial
    budget = f'budgets/multi-user-baseline/{variant}/{variant}/calls.sqlite'
    scope = {'experiment_id': 'multi-user-baseline', 'variant': variant,
             'user_id': 'user-a', 'session_id': 'session-a'}
    worker = stopped_container('a' * 64, 'worker', 'harness-lab-' + trial + '-main-a-worker-a',
                               trial=trial, image='worker')
    worker['mounts'] = [{'Destination': '/config/model-run.token', 'Source': str((tmp_path / budget).with_name('run.token')),
                         'RW': False}]
    proxy = stopped_container('b' * 64, 'model-proxy', 'harness-lab-' + trial + '-main-proxy-a', trial=trial)
    proxy['mounts'] = [{'Destination': '/budget', 'Source': str((tmp_path / budget).parent), 'RW': True}]
    grade = stopped_container('c' * 64, 'acceptance', 'harness-lab-' + trial + '-bench-3',
                              trial=trial, image='bench', exit_code=1)
    registry = {'registrations': {item['id']: deepcopy(item) for item in (worker, proxy)},
                'retirements': {item['id']: {'after_stop': deepcopy(item),
                    'deleted': {'id': item['id'], 'id_absent': True, 'observed_at': '2026-10-21T00:00:01Z'}}
                    for item in (worker, proxy)}}
    documents = {
        'plan.json': {'schema': 'harness-lab-multi-user-plan/1', 'trial_id': trial, 'candidate': 'deepagents_langgraph',
            'date': '2026-10-21', 'baseline_only': True, 'executed_tasks': ['task-a'],
            'tasks': [{'task_id': 'task-a', 'kind': 'main', 'owner': 'user-a', 'scope': scope,
                       'run': '/data/' + relative.rsplit('/', 1)[0] + '/' + variant}],
            'pins': {'bench_image_id': 'bench', 'worker_image_id': 'worker', 'services_image_id': 'services'}},
        'host-result.json': {'baseline_only': True, 'passed': False, 'errors': ['failed'],
                             'sampling_errors': [], 'resource_overages': []},
        'baseline-verdict.json': {'schema': 'harness-lab-multi-user-baseline/1', 'candidate': 'deepagents_langgraph',
            'baseline_only': True, 'executed_tasks': ['task-a'], 'passed': False, 'i24_capability_verified': False},
        'container-transition-registry.json': registry,
        'bench-3-baseline-grade.container.json': grade,
    }
    entry = {'run_path': relative, 'run_id': trial, 'candidate': 'deepagents_langgraph', 'case': 'scope-baseline',
             'budget_group': 'multi-user-baseline', 'budget_path': budget, 'task_id': 'task-a',
             'closure_kind': I24_CLOSURE, 'upstream_kind': 'remote'}
    if change == 'active':
        registry['retirements'][proxy['id']]['after_stop']['state']['Running'] = True
    elif change == 'not-deleted':
        registry['retirements'][proxy['id']]['deleted']['id_absent'] = False
    elif change == 'foreign-container':
        registry['retirements'][worker['id']]['after_stop']['trial_id'] = 'other'
    elif change == 'wrong-token-mount':
        registry['registrations'][worker['id']]['mounts'][0]['Source'] = '/other/run.token'
    elif change == 'foreign-scope':
        scope['user_id'] = 'other'
    elif change == 'more-writers':
        registry['registrations']['d' * 64] = deepcopy(proxy)
    elif change == 'full-scope':
        documents['plan.json']['baseline_only'] = False
    elif change == 'other-budget':
        entry['budget_path'] = budget.replace(variant, 'other')
    elif change == 'nested-metadata':
        documents['protected/plan.json'] = {}
    elif change == 'malformed-tasks':
        documents['plan.json']['tasks'] = {}
    elif change == 'malformed-retirement':
        registry['retirements'][worker['id']] = []
    entry = freeze(tmp_path, entry, documents)
    if change == 'modified':
        (tmp_path / relative / 'host-result.json').write_text('{}')
    return entry


def test_i24_failed_baseline_with_deleted_writers_is_closed_spend(tmp_path):
    facts = closure(tmp_path, i24_enrollment(tmp_path))
    assert facts['original_verdict_passed'] is facts['original_host_passed'] is False
    assert facts['writer_exit_codes'] == {'worker': 137, 'model-proxy': 137}
    assert facts['budget_writers_closed'] is True and facts['cleanup_independently_proven'] is False


@pytest.mark.parametrize('change', ['active', 'not-deleted', 'foreign-container', 'wrong-token-mount',
    'foreign-scope', 'more-writers', 'full-scope', 'other-budget', 'nested-metadata', 'modified',
    'malformed-tasks', 'malformed-retirement'])
def test_i24_unbound_or_active_writers_are_not_closed_spend(tmp_path, change):
    with pytest.raises(ValueError):
        closure(tmp_path, i24_enrollment(tmp_path, change))


def i25_enrollment(tmp_path, change=None):
    trial, owner, kind = 'closed-i25', 'user-001', 'baseline'
    task_id = kind + '-' + owner
    relative = 'runs/2026-10-21/deepagents_langgraph/instance-cost/' + trial
    budget = relative + f'/users/{owner}/budgets/{kind}/calls.sqlite'
    scope = {'experiment_id': 'instance-cost', 'variant': trial + '-' + task_id,
             'user_id': owner, 'session_id': task_id}
    proxy = stopped_container('a' * 64, 'model-proxy', f'hl-ic-{trial}-{kind}-proxy-{owner}')
    proxy['mounts'] = [{'Destination': '/budget', 'Source': str((tmp_path / budget).parent), 'RW': True}]
    registered = deepcopy(proxy)
    registered['state']['Status'] = 'created'
    grade = stopped_container('b' * 64, 'bench', f'hl-ic-{trial}-bench-16', image='bench', exit_code=1)
    documents = {
        'protected/plan.json': {'schema': 'harness-lab-instance-cost/1', 'flow_schema': 'harness-lab-instance-cost-flow/1',
            'candidate': 'deepagents_langgraph', 'trial_id': trial, 'pair_id': 'pair', 'strategy': 'on-demand',
            'users': 2, 'seed': 101, 'baseline_only': True, 'active_users': [owner, 'user-002'],
            'bench_image_id': 'bench', 'services_image_id': 'services'},
        'protected/tasks.json': [{'task_id': task_id, 'owner': owner, 'kind': kind, 'scope': scope}],
        'protected/flow-host.json': {'task_timings': [], 'normal_closes': []},
        'protected/baseline-verdict.json': {'schema': 'harness-lab-instance-cost-flow/1',
            'candidate': 'deepagents_langgraph', 'trial_id': trial, 'pair_id': 'pair', 'strategy': 'on-demand',
            'users': 2, 'seed': 101, 'cost_subset_passed': False, 'i25_complete': False},
        'protected/flow-cleanup.json': {'unconfirmed_resources': []},
        'protected/final-containers.json': [],
        'protected/grade-flow-container.json': grade,
        'protected/lifecycle-8.json': {'observed': registered, 'pin': {'id': proxy['id'], 'role': 'model-proxy',
            'image_id': 'services', 'running': False}},
        'protected/retired-11.json': proxy,
    }
    entry = {'run_path': relative, 'run_id': trial, 'candidate': 'deepagents_langgraph', 'case': 'instance-cost',
             'budget_group': 'instance-cost', 'budget_path': budget, 'task_id': task_id,
             'closure_kind': I25_CLOSURE, 'upstream_kind': 'remote',
             'proxy_created_source': 'protected/lifecycle-8.json', 'proxy_retired_source': 'protected/retired-11.json'}
    if change == 'not-clean':
        documents['protected/flow-cleanup.json']['unconfirmed_resources'] = ['leftover']
    elif change == 'not-empty':
        documents['protected/final-containers.json'] = [proxy]
    elif change == 'active':
        proxy['state']['Running'] = True
    elif change == 'foreign-id':
        proxy['id'] = 'c' * 64
    elif change == 'foreign-scope':
        scope['user_id'] = 'other'
    elif change == 'wrong-mount':
        registered['mounts'][0]['Source'] = '/other/budget'
    elif change == 'active-grade':
        grade['state']['Running'] = True
    elif change == 'other-budget':
        entry['budget_path'] = budget.replace('/baseline/', '/wake/')
    elif change == 'wrong-source':
        entry['proxy_created_source'] = 'protected/config.json'
    elif change == 'arbitrary-nested-source':
        documents['protected/other/plan.json'] = {}
    elif change == 'missing-cleanup':
        del documents['protected/flow-cleanup.json']
    elif change == 'malformed-tasks':
        documents['protected/tasks.json'] = {}
    elif change == 'malformed-state':
        proxy['state'] = []
    elif change == 'malformed-mounts':
        registered['mounts'] = ['not-a-mount']
    elif change == 'inactive-baseline':
        documents['protected/plan.json']['active_users'] = ['user-002', owner]
    entry = freeze(tmp_path, entry, documents)
    if change == 'modified':
        (tmp_path / relative / 'protected/tasks.json').write_text('[]')
    return entry


def test_i25_failed_flow_and_137_proxy_exit_still_close_spend(tmp_path):
    facts = closure(tmp_path, i25_enrollment(tmp_path))
    assert facts['original_verdict_passed'] is False and facts['proxy_exit_code'] == 137
    assert facts['grade_exit_code'] == 1 and facts['cleanup_independently_proven'] is True


@pytest.mark.parametrize('change', ['not-clean', 'not-empty', 'active', 'foreign-id', 'foreign-scope',
    'wrong-mount', 'active-grade', 'other-budget', 'wrong-source', 'arbitrary-nested-source', 'missing-cleanup', 'modified',
    'malformed-tasks', 'malformed-state', 'malformed-mounts', 'inactive-baseline'])
def test_i25_unfinished_or_unbound_flow_is_not_closed_spend(tmp_path, change):
    with pytest.raises((ValueError, KeyError)):
        closure(tmp_path, i25_enrollment(tmp_path, change))


def i24_scope_enrollment(tmp_path, change=None, task_id='task-a'):
    trial = 'closed-i24-scope'
    relative = 'runs/2026-10-21/deepagents_langgraph/scope/' + trial
    run = tmp_path / relative
    bindings = [('task-a', 'main', 'a'), ('task-b', 'main', 'b'),
                ('task-a-cancel', 'cancel', 'a'), ('task-b-cancel', 'cancel', 'b')]
    registry = {'registrations': {}, 'retirements': {}}
    tasks, chains, identities = [], {}, {}

    def container(role, suffix, mounts=None, *, routed=True):
        identity = format(len(registry['registrations']) + 1, '064x')
        name = 'harness-lab-' + trial + '-' + suffix
        image = 'worker' if role == 'worker' else 'bench' if role == 'acceptance' else 'services'
        after = stopped_container(identity, role, name, trial=trial, image=image)
        after['environment_names'] = ['MODEL_API_KEY'] if role == 'model-proxy' else ['PATH']
        after['mounts'] = [{'Destination': destination, 'Source': str(source), 'RW': writable}
                           for destination, (source, writable) in (mounts or {}).items()]
        after['state'].update(StartedAt='2026-10-21T00:00:00Z', FinishedAt='2026-10-21T00:00:01Z',
                              OOMKilled=False, Dead=False, Error='')
        registered = deepcopy(after)
        registered['state'].update(Status='created', StartedAt='0001-01-01T00:00:00Z')
        registry['registrations'][identity] = registered
        identities[suffix] = identity
        if routed:
            before = deepcopy(after)
            before['state'].update(Status='running', Running=True, Pid=123)
            registry['retirements'][identity] = {'before_stop': before, 'after_stop': after,
                'deleted': {'id': identity, 'id_absent': True, 'observed_at': '2026-10-21T00:00:02Z'}}
        return after

    for current_task, kind, letter in bindings:
        variant = trial + '-' + kind + '-' + letter
        scope = {'experiment_id': 'multi-user', 'variant': variant,
                 'user_id': 'user-' + letter, 'session_id': 'session-' + letter}
        task_run = run.parent / variant
        tasks.append({'task_id': current_task, 'kind': kind, 'owner': scope['user_id'], 'scope': scope,
                      'run': '/data/' + task_run.relative_to(tmp_path).as_posix()})
        chains[current_task] = {'schema': 'harness-lab-pause-chain/1', 'specialty': 'multi-user',
            'candidate': 'deepagents_langgraph', 'chain_id': variant, 'scope': deepcopy(scope)}
        budget = tmp_path / f'budgets/multi-user/{variant}/{variant}'
        fixture = tmp_path / 'fixtures/multi-user' / variant
        container('model-proxy', kind + '-proxy-' + letter, {'/budget': (budget, True)})
        for attempt in (['worker-a', 'worker-b'] if kind == 'main' else ['worker-a']):
            target = fixture / 'attempts' / attempt
            mounts = {'/state': (tmp_path / 'state/multi-user' / variant / 'deepagents_langgraph' / scope['user_id'] / scope['session_id'], True),
                '/workspace': (tmp_path / 'workspaces/multi-user' / variant / 'deepagents_langgraph' / scope['user_id'] / scope['session_id'], True),
                '/evidence': (task_run / 'attempts' / attempt / 'candidate', True),
                '/worker-private': (task_run / 'attempts' / attempt / 'private', True),
                '/control/policy.json': (target / 'worker-policy.json', False),
                '/control/worker.token': (target / 'worker.token', False),
                '/config/request.json': (target / 'request.json', False),
                '/config/lifecycle.json': (target / 'lifecycle.json', False),
                '/config/scope.json': (fixture / 'scope.json', False),
                '/config/scope.token': (fixture / 'scope.token', False),
                '/config/model-run.token': (budget / 'run.token', False)}
            container('worker', kind + '-' + letter + '-' + attempt, mounts)
    for kind in ('main', 'cancel'):
        mounts = {'/control/backend.json': (tmp_path / 'fixtures/multi-user' / trial / ('backend-' + kind + '.json'), False),
                  '/backend-private': (run / ('backend-' + kind), True)}
        mounts.update({'/data/' + folder: (tmp_path / folder, False)
                       for folder in ('state', 'workspaces', 'fixtures', 'budgets', 'runs')})
        for source in (run / 'clients', run / ('backend-' + kind),
                       run.parent / (trial + '-' + kind + '-a') / 'trusted',
                       run.parent / (trial + '-' + kind + '-b') / 'trusted'):
            mounts['/data/' + source.relative_to(tmp_path).as_posix()] = (source, True)
        container('backend', kind + '-backend', mounts)
        for letter in ('a', 'b'):
            container('tools', kind + '-tools-' + letter, routed=False)
    container('fixtures', 'fixtures', routed=False)
    for sequence in range(1, 7):
        container('acceptance', 'bench-' + str(sequence), routed=False)
    grade = stopped_container('f' * 64, 'acceptance', 'harness-lab-' + trial + '-bench-7',
                               trial=trial, image='bench', exit_code=1)
    grade['state'].update(StartedAt='2026-10-21T00:00:03Z', FinishedAt='2026-10-21T00:00:04Z')
    documents = {
        'plan.json': {'schema': 'harness-lab-multi-user-plan/1', 'trial_id': trial, 'candidate': 'deepagents_langgraph',
            'date': '2026-10-21', 'baseline_only': False, 'executed_tasks': [task for task, _, _ in bindings],
            'tasks': tasks, 'pins': {'bench_image_id': 'bench', 'worker_image_id': 'worker', 'services_image_id': 'services'}},
        'host-result.json': {'baseline_only': False, 'passed': False, 'errors': [],
                             'sampling_errors': [{'reason_code': 'transition_terminal_not_healthy'}], 'resource_overages': []},
        'scope-verdict.json': {'schema': 'harness-lab-multi-user-verdict/1', 'candidate': 'deepagents_langgraph',
            'passed': False, 'tasks': {task: {'passed': False} for task, _, _ in bindings}},
        'container-transition-registry.json': registry,
        'bench-7-grade.container.json': grade,
    }
    selected = next(task for task in tasks if task['task_id'] == task_id)
    variant = selected['scope']['variant']
    entry = {'run_path': relative, 'run_id': trial, 'candidate': 'deepagents_langgraph', 'case': 'scope',
             'budget_group': 'multi-user', 'budget_path': f'budgets/multi-user/{variant}/{variant}/calls.sqlite',
             'task_id': task_id, 'closure_kind': I24_SCOPE_CLOSURE, 'upstream_kind': 'remote'}
    proxy_id, worker_id, backend_id = (identities[name] for name in
                                     ('main-proxy-a', 'main-a-worker-b', 'cancel-backend'))
    proxy, worker, backend = (registry['retirements'][identity] for identity in (proxy_id, worker_id, backend_id))
    if change == 'oom':
        worker['after_stop']['state']['OOMKilled'] = True
    elif change == 'active-proxy':
        proxy['after_stop']['state']['Running'] = True
    elif change == 'active-backend':
        backend['after_stop']['state']['Status'] = 'running'
    elif change == 'undeleted-backend':
        backend['deleted']['id_absent'] = False
    elif change == 'undeleted-replacement':
        del worker['deleted']
    elif change == 'wrong-deleted-id':
        worker['deleted']['id'] = proxy_id
    elif change == 'missing-replacement':
        del registry['registrations'][worker_id]
    elif change == 'extra-writer':
        registry['registrations']['e' * 64] = deepcopy(registry['registrations'][proxy_id])
    elif change == 'foreign-retirement':
        proxy['after_stop']['trial_id'] = 'other'
    elif change == 'wrong-image':
        backend['after_stop']['image_id'] = 'other'
    elif change == 'reused-name-new-id':
        worker['after_stop']['id'] = 'e' * 64
    elif change == 'wrong-worker-budget':
        next(m for m in registry['registrations'][worker_id]['mounts']
             if m['Destination'] == '/config/model-run.token')['Source'] = '/other/run.token'
    elif change == 'wrong-proxy-budget':
        proxy['after_stop']['mounts'][0]['Source'] = '/other/budget'
    elif change == 'backend-budget-writable':
        next(m for m in backend['after_stop']['mounts'] if m['Destination'] == '/data/budgets')['RW'] = True
    elif change == 'backend-new-route':
        backend['after_stop']['mounts'].append({'Destination': '/var/run/docker.sock', 'Source': '/var/run/docker.sock', 'RW': True})
    elif change == 'nonproxy-key':
        registry['registrations'][identities['fixtures']]['environment_names'].append('MODEL_API_KEY')
    elif change == 'different-start':
        worker['before_stop']['state']['StartedAt'] = '2026-10-20T00:00:00Z'
    elif change == 'deleted-after-grade':
        backend['deleted']['observed_at'] = '2026-10-21T00:00:04Z'
    elif change == 'missing-start':
        del proxy['after_stop']['state']['StartedAt']
    elif change == 'unknown-oom':
        del worker['after_stop']['state']['OOMKilled']
    elif change == 'active-grade':
        grade['state']['Running'] = True
    elif change == 'no-timezone':
        grade['state']['StartedAt'] = '2026-10-21T00:00:03'
    elif change == 'wrong-scope':
        chains['task-b']['scope']['user_id'] = 'user-a'
    elif change == 'missing-task':
        tasks.pop()
    elif change == 'baseline':
        documents['plan.json']['baseline_only'] = True
    elif change == 'unfinished-host':
        del documents['host-result.json']['errors']
    elif change == 'wrong-budget':
        entry['budget_path'] = entry['budget_path'].replace('main-a', 'main-b')
    elif change == 'arbitrary-nested-source':
        documents['protected/other.json'] = {}
    entry = freeze(tmp_path, entry, documents)
    for current_task, kind, letter in bindings:
        path = run.parent / (trial + '-' + kind + '-' + letter) / 'chain.json'
        path.parent.mkdir(parents=True)
        data = (json.dumps(chains[current_task]) + '\n').encode()
        path.write_bytes(data)
        entry['closure_sources'].append({'path': path.relative_to(tmp_path).as_posix(),
                                         'sha256': hashlib.sha256(data).hexdigest()})
    if change == 'foreign-chain-path':
        entry['closure_sources'][-1]['path'] = entry['closure_sources'][-1]['path'].replace('-cancel-b/', '-other/')
    elif change == 'modified':
        (run / 'host-result.json').write_text('{}')
    return entry


@pytest.mark.parametrize('task_id', ['task-a', 'task-b', 'task-a-cancel', 'task-b-cancel'])
def test_i24_failed_scope_closes_each_budget_only_after_all_paid_routes_are_deleted(tmp_path, task_id):
    facts = closure(tmp_path, i24_scope_enrollment(tmp_path, task_id=task_id))
    assert facts['original_verdict_passed'] is facts['original_host_passed'] is False
    assert len(facts['writer_exits']) == 10 and len(facts['backend_exits']) == 2
    assert all(row['exit_code'] == 137 for row in facts['writer_exits'].values())
    assert facts['paid_route_cleanup_proven'] is facts['budget_writers_closed'] is True
    assert facts['cleanup_independently_proven'] is facts['whole_trial_cleanup'] is facts['exit_health_claimed'] is False


def test_i24_scope_oom_is_retained_as_failed_spend_without_a_health_claim(tmp_path):
    facts = closure(tmp_path, i24_scope_enrollment(tmp_path, 'oom'))
    assert sum(row['oom_killed'] for row in facts['writer_exits'].values()) == 1
    assert facts['original_host_passed'] is False and facts['exit_health_claimed'] is False


@pytest.mark.parametrize('change', ['active-proxy', 'active-backend', 'undeleted-backend', 'undeleted-replacement',
    'wrong-deleted-id', 'missing-replacement', 'extra-writer', 'foreign-retirement', 'wrong-image', 'reused-name-new-id',
    'wrong-worker-budget', 'wrong-proxy-budget', 'backend-budget-writable', 'backend-new-route', 'nonproxy-key',
    'different-start', 'deleted-after-grade', 'missing-start', 'unknown-oom', 'active-grade', 'no-timezone',
    'wrong-scope', 'missing-task', 'baseline', 'unfinished-host', 'wrong-budget', 'arbitrary-nested-source',
    'foreign-chain-path', 'modified'])
def test_i24_scope_does_not_infer_closure_from_partial_or_foreign_evidence(tmp_path, change):
    with pytest.raises((ValueError, KeyError)):
        closure(tmp_path, i24_scope_enrollment(tmp_path, change))
