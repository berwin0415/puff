"""Read an explicit research-budget inventory using only safe metadata columns.

No candidate, Docker command, model call, response text, environment or credential
is loaded. New/unlisted databases are reported by path, never silently enrolled.
"""

import argparse
from collections import Counter, defaultdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3


COLUMNS = ('id', 'started_at', 'path', 'outcome', 'upstream_status')
ALLOWED_METADATA = {'manifest.json', 'verdict.json', 'orchestration.json', 'cleanup.json',
                    'budget-probe-manifest.json', 'budget-probe-verdict.json', 'host.json', 'cleanup-followup.json', 'chain.json'}
BUCKETS = ('remote_forwarded_2xx', 'remote_forwarded_non2xx', 'mock_forwarded',
           'local_injected', 'recorded_rejected', 'remote_attempt_unconfirmed', 'unknown_or_inflight')
I24_CLOSURE = 'finished-i24-baseline-budget-writers'
I24_SCOPE_CLOSURE = 'finished-i24-scope-budget-writers-and-routes'
I25_CLOSURE = 'finished-instance-cost-flow-and-cleanup'
I24_METADATA = {'plan.json', 'host-result.json', 'baseline-verdict.json',
                'container-transition-registry.json', 'bench-3-baseline-grade.container.json'}
I24_SCOPE_METADATA = {'plan.json', 'host-result.json', 'scope-verdict.json',
                      'container-transition-registry.json', 'bench-7-grade.container.json'}
I25_METADATA = {'protected/plan.json', 'protected/tasks.json', 'protected/flow-host.json',
                'protected/baseline-verdict.json', 'protected/flow-verdict.json',
                'protected/flow-cleanup.json', 'protected/final-containers.json',
                'protected/grade-flow-container.json'}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha(data):
    return hashlib.sha256(data).hexdigest()


def path_in(root, relative):
    require(isinstance(relative, str), 'Inventory path must be a string')
    part = Path(relative)
    require(not part.is_absolute() and '..' not in part.parts and 'old' not in part.parts,
            'Inventory path is not current evidence')
    path = root / part
    require(not any(item.is_symlink() for item in (path, *path.parents)), 'Evidence cannot follow links')
    require(path.resolve().is_relative_to(root), 'Evidence path escapes data root')
    return path


def load_json(path, *, allow_list=False):
    require(path.is_file() and path.stat().st_size <= 32 * 1024**2, 'Missing or oversized JSON metadata')
    data = path.read_bytes()
    value = json.loads(data)
    require(isinstance(value, dict) or (allow_list and isinstance(value, list)), 'Unexpected JSON metadata shape')
    return value, sha(data)


def metadata_names(entry):
    """Each closure gets an explicit whitelist; protected/ is not a general exception."""
    if entry['closure_kind'] == I24_CLOSURE:
        return I24_METADATA
    if entry['closure_kind'] == I24_SCOPE_CLOSURE:
        return I24_SCOPE_METADATA | {f'tasks/{task}/chain.json' for task in
                                     ('task-a', 'task-b', 'task-a-cancel', 'task-b-cancel')}
    if entry['closure_kind'] == I25_CLOSURE:
        created, retired = entry['proxy_created_source'], entry['proxy_retired_source']
        require(isinstance(created, str) and re.fullmatch(r'protected/lifecycle-[1-9][0-9]*\.json', created)
                and isinstance(retired, str) and re.fullmatch(r'protected/retired-[1-9][0-9]*\.json', retired),
                'Unexpected proxy lifecycle source')
        return I25_METADATA | {created, retired}
    return ALLOWED_METADATA


def terminal(container):
    """Stopped execution is sufficient for spend; exit health is a separate claim."""
    require(isinstance(container, dict), 'Unexpected terminal container metadata')
    state = container.get('state', {})
    require(isinstance(state, dict) and state.get('Status') == 'exited' and state.get('Running') is False
            and state.get('Restarting') is False and state.get('Pid') == 0
            and type(state.get('ExitCode')) is int
            and isinstance(state.get('FinishedAt'), str) and state['FinishedAt']
            and not state['FinishedAt'].startswith('0001-'), 'Container execution is not terminal')
    return state['ExitCode']


def exact_container(container, *, identity, role, image, name, trial=None):
    require(isinstance(container, dict) and isinstance(identity, str) and re.fullmatch(r'[0-9a-f]{64}', identity)
            and container.get('id') == identity and container.get('role') == role
            and container.get('image_id') == image and container.get('name') == '/' + name
            and (trial is None or container.get('trial_id') == trial), 'Container ownership is unproven')


def exact_mount(container, destination, source, *, writable):
    original = container.get('mounts', [])
    require(isinstance(original, list) and all(isinstance(row, dict) for row in original),
            'Unexpected mount metadata')
    mounts = [row for row in original if row.get('Destination') == destination]
    require(len(mounts) == 1 and mounts[0].get('Source') == str(source)
            and mounts[0].get('RW') is writable, 'Budget mount ownership is unproven')


def exact_mounts(container, expected):
    mounts = container.get('mounts')
    require(isinstance(mounts, list) and len(mounts) == len(expected)
            and all(isinstance(row, dict) for row in mounts), 'Unexpected scope mount set')
    for destination, (source, writable) in expected.items():
        exact_mount(container, destination, source, writable=writable)


def instant(value):
    require(isinstance(value, str) and value and not value.startswith('0001-'), 'Missing execution timestamp')
    parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
    require(parsed.tzinfo is not None and parsed.utcoffset() is not None, 'Execution timestamp needs a timezone')
    return parsed


def scope_task_bindings(run):
    return {task: {'kind': kind, 'letter': letter, 'variant': run.name + '-' + kind + '-' + letter}
            for task, kind, letter in (('task-a', 'main', 'a'), ('task-b', 'main', 'b'),
                                      ('task-a-cancel', 'cancel', 'a'), ('task-b-cancel', 'cancel', 'b'))}


def i24_scope_closure(root, run, entry, sources):
    """Close the fixed four-task paid routes, without claiming whole-trial cleanup."""
    plan, host, verdict, registry, grade = (sources[name] for name in (
        'plan.json', 'host-result.json', 'scope-verdict.json',
        'container-transition-registry.json', 'bench-7-grade.container.json'))
    trial, bindings = entry['run_id'], scope_task_bindings(run)
    require(plan.get('schema') == 'harness-lab-multi-user-plan/1' and plan.get('trial_id') == trial
            and plan.get('candidate') == entry['candidate'] == 'deepagents_langgraph'
            and entry['case'] == 'scope' and entry['budget_group'] == 'multi-user'
            and plan.get('date') == run.parents[2].name and plan.get('baseline_only') is False
            and plan.get('executed_tasks') == list(bindings) and entry['task_id'] in bindings
            and entry['upstream_kind'] == 'remote', 'I24 scope identity is unproven')
    require(isinstance(plan.get('tasks'), list) and len(plan['tasks']) == len(bindings)
            and all(isinstance(task, dict) for task in plan['tasks'])
            and {task.get('task_id') for task in plan['tasks']} == set(bindings), 'I24 scope tasks are incomplete')
    require(host.get('baseline_only') is False and type(host.get('passed')) is bool
            and all(isinstance(host.get(key), list) for key in ('errors', 'sampling_errors', 'resource_overages'))
            and verdict.get('schema') == 'harness-lab-multi-user-verdict/1'
            and verdict.get('candidate') == entry['candidate'] and type(verdict.get('passed')) is bool
            and isinstance(verdict.get('tasks'), dict) and set(verdict['tasks']) == set(bindings)
            and all(isinstance(result, dict) and type(result.get('passed')) is bool
                    for result in verdict['tasks'].values()), 'I24 scope finalization is unproven')
    expected = {}
    for task_id, binding in bindings.items():
        kind, letter, variant = (binding[key] for key in ('kind', 'letter', 'variant'))
        scope = {'experiment_id': 'multi-user', 'variant': variant,
                 'user_id': 'user-' + letter, 'session_id': 'session-' + letter}
        task = next(task for task in plan['tasks'] if task['task_id'] == task_id)
        task_run = run.parent / variant
        chain = sources[f'tasks/{task_id}/chain.json']
        require(task.get('scope') == scope and task.get('kind') == kind and task.get('owner') == scope['user_id']
                and task.get('run') == '/data/' + task_run.relative_to(root).as_posix()
                and chain.get('schema') == 'harness-lab-pause-chain/1' and chain.get('specialty') == 'multi-user'
                and chain.get('candidate') == entry['candidate'] and chain.get('chain_id') == variant
                and chain.get('scope') == scope, 'I24 task scope is unproven')
        budget = root / f'budgets/multi-user/{variant}/{variant}'
        if task_id == entry['task_id']:
            require(entry['budget_path'] == (budget / 'calls.sqlite').relative_to(root).as_posix(),
                    'I24 scope budget ownership is unproven')
        fixture = root / 'fixtures/multi-user' / variant
        expected[kind + '-proxy-' + letter] = ('model-proxy', plan['pins']['services_image_id'],
                                              {'/budget': (budget, True)}, task_id)
        for attempt in (('worker-a', 'worker-b') if kind == 'main' else ('worker-a',)):
            target = fixture / 'attempts' / attempt
            mounts = {'/config/model-run.token': (budget / 'run.token', False),
                      '/config/scope.json': (fixture / 'scope.json', False),
                      '/config/scope.token': (fixture / 'scope.token', False),
                      '/state': (root / 'state/multi-user' / variant / entry['candidate'] / scope['user_id'] / scope['session_id'], True),
                      '/workspace': (root / 'workspaces/multi-user' / variant / entry['candidate'] / scope['user_id'] / scope['session_id'], True),
                      '/evidence': (task_run / 'attempts' / attempt / 'candidate', True),
                      '/worker-private': (task_run / 'attempts' / attempt / 'private', True),
                      '/control/policy.json': (target / 'worker-policy.json', False),
                      '/control/worker.token': (target / 'worker.token', False),
                      '/config/request.json': (target / 'request.json', False),
                      '/config/lifecycle.json': (target / 'lifecycle.json', False)}
            expected[kind + '-' + letter + '-' + attempt] = ('worker', plan['pins']['worker_image_id'], mounts, task_id)
    # A stopped/deleted backend cannot dispatch a late native child through an
    # otherwise surviving Worker. Every known route and paid execution closes
    # before the grade starts; the fixed host recipe only grades/cleans after it.
    for kind in ('main', 'cancel'):
        mounts = {'/control/backend.json': (root / 'fixtures/multi-user' / trial / ('backend-' + kind + '.json'), False),
                  '/backend-private': (run / ('backend-' + kind), True)}
        mounts.update({'/data/' + folder: (root / folder, False)
                       for folder in ('state', 'workspaces', 'fixtures', 'budgets', 'runs')})
        for folder in ('clients', 'backend-' + kind):
            source = run / folder
            mounts['/data/' + source.relative_to(root).as_posix()] = (source, True)
        for letter in ('a', 'b'):
            source = run.parent / (trial + '-' + kind + '-' + letter) / 'trusted'
            mounts['/data/' + source.relative_to(root).as_posix()] = (source, True)
        expected[kind + '-backend'] = ('backend', plan['pins']['services_image_id'], mounts, None)
    registrations, retirements = registry.get('registrations'), registry.get('retirements')
    require(isinstance(registrations, dict) and isinstance(retirements, dict)
            and all(isinstance(row, dict) for row in registrations.values()), 'Unexpected I24 scope registry')
    other_names = {'/harness-lab-' + trial + '-fixtures': 'fixtures'}
    other_names.update({'/harness-lab-' + trial + '-' + kind + '-tools-' + letter: 'tools'
                        for kind in ('main', 'cancel') for letter in ('a', 'b')})
    other_names.update({'/harness-lab-' + trial + '-bench-' + str(sequence): 'acceptance'
                        for sequence in range(1, 7)})
    routed = [(identity, row) for identity, row in registrations.items()
              if row.get('role') in ('worker', 'model-proxy', 'backend')]
    names = {'/harness-lab-' + trial + '-' + suffix: value for suffix, value in expected.items()}
    require(len(routed) == len(names) and {row.get('name') for _, row in routed} == set(names),
            'I24 paid writers or dispatch routes are incomplete')
    require(len(registrations) == len(names) + len(other_names)
            and {row.get('name') for row in registrations.values()} == set(names) | set(other_names),
            'I24 scope registered an unexpected execution container')
    for identity, registered in registrations.items():
        require(isinstance(registered.get('environment_names'), list)
                and (registered.get('role') == 'model-proxy' or 'MODEL_API_KEY' not in registered['environment_names']),
                'I24 remote model writer is outside the fixed proxies')
        if registered.get('name') in other_names:
            role = other_names[registered['name']]
            exact_container(registered, identity=identity, role=role,
                            image=plan['pins']['bench_image_id' if role == 'acceptance' else 'services_image_id'],
                            name=registered['name'][1:], trial=trial)
    exact_container(grade, identity=grade.get('id'), role='acceptance', image=plan['pins']['bench_image_id'],
                    name='harness-lab-' + trial + '-bench-7', trial=trial)
    grade_exit, grade_start = terminal(grade), instant(grade.get('state', {}).get('StartedAt'))
    writers, routes = {}, {}
    for identity, registered in routed:
        role, image, mounts, task_id = names[registered['name']]
        arguments = dict(identity=identity, role=role, image=image, name=registered['name'][1:], trial=trial)
        exact_container(registered, **arguments)
        require(isinstance(registered.get('state'), dict) and registered['state'].get('Status') == 'created'
                and registered['state'].get('Running') is False, 'I24 original route registration is missing')
        retirement = retirements.get(identity)
        require(isinstance(retirement, dict) and isinstance(retirement.get('deleted'), dict),
                'I24 route retirement is missing')
        before, stopped, deleted = retirement.get('before_stop'), retirement.get('after_stop'), retirement['deleted']
        exact_container(before, **arguments)
        exact_container(stopped, **arguments)
        for observation in (registered, before, stopped):
            exact_mounts(observation, mounts)
        exit_code, state = terminal(stopped), stopped['state']
        require(isinstance(before.get('state'), dict) and before['state'].get('StartedAt') == state.get('StartedAt')
                and instant(state.get('StartedAt')) <= instant(state['FinishedAt'])
                and deleted.get('id') == identity and deleted.get('id_absent') is True
                and instant(state['FinishedAt']) <= instant(deleted.get('observed_at')) < grade_start,
                'I24 route is not deleted before final grading')
        require(type(state.get('OOMKilled')) is bool and type(state.get('Dead')) is bool
                and isinstance(state.get('Error'), str), 'I24 original exit facts are missing')
        facts = {'id': identity, 'task_id': task_id, 'role': role, 'exit_code': exit_code,
                 'oom_killed': state['OOMKilled'], 'dead': state['Dead'], 'error': state['Error']}
        (routes if role == 'backend' else writers)[registered['name'][1:]] = facts
    return {'original_verdict_passed': verdict['passed'], 'original_host_passed': host['passed'],
            'grade_exit_code': grade_exit, 'writer_exits': writers, 'backend_exits': routes,
            'budget_writers_closed': True, 'paid_route_cleanup_proven': True,
            'cleanup_independently_proven': False, 'whole_trial_cleanup': False, 'exit_health_claimed': False,
            'claim': 'closed spend; all fixed paid writers and backend routes deleted before grade; '
                     'no task success, exit health, capability or whole-trial cleanup inferred'}


def i24_closure(root, run, entry, sources):
    plan, host, verdict, registry, grade = (sources[name] for name in (
        'plan.json', 'host-result.json', 'baseline-verdict.json',
        'container-transition-registry.json', 'bench-3-baseline-grade.container.json'))
    trial = entry['run_id']
    variant = trial + '-main-a'
    scope = {'experiment_id': 'multi-user-baseline', 'variant': variant,
             'user_id': 'user-a', 'session_id': 'session-a'}
    require(plan.get('schema') == 'harness-lab-multi-user-plan/1'
            and plan.get('trial_id') == trial and plan.get('candidate') == entry['candidate'] == 'deepagents_langgraph'
            and entry['case'] == 'scope-baseline' and entry['budget_group'] == 'multi-user-baseline'
            and plan.get('date') == run.parents[2].name and plan.get('baseline_only') is True
            and plan.get('executed_tasks') == ['task-a'] and entry['task_id'] == 'task-a'
            and entry['upstream_kind'] == 'remote', 'I24 baseline identity is unproven')
    require(isinstance(plan.get('tasks'), list) and all(isinstance(row, dict) for row in plan['tasks']),
            'Unexpected I24 task metadata')
    tasks = [row for row in plan['tasks'] if row.get('task_id') == entry['task_id']]
    require(len(tasks) == 1 and tasks[0].get('scope') == scope and tasks[0].get('kind') == 'main'
            and tasks[0].get('owner') == 'user-a'
            and tasks[0].get('run') == '/data/' + str(run.parent.relative_to(root) / variant),
            'I24 task ownership is unproven')
    expected = f'budgets/multi-user-baseline/{variant}/{variant}/calls.sqlite'
    require(entry['budget_path'] == expected and host.get('baseline_only') is True
            and type(host.get('passed')) is bool
            and all(isinstance(host.get(key), list) for key in ('errors', 'sampling_errors', 'resource_overages'))
            and verdict.get('schema') == 'harness-lab-multi-user-baseline/1'
            and verdict.get('candidate') == entry['candidate'] and verdict.get('baseline_only') is True
            and verdict.get('executed_tasks') == ['task-a'] and type(verdict.get('passed')) is bool
            and verdict.get('i24_capability_verified') is False, 'I24 completion or budget binding is unproven')
    exact_container(grade, identity=grade.get('id'), role='acceptance', image=plan['pins']['bench_image_id'],
                    name='harness-lab-' + trial + '-bench-3', trial=trial)
    grade_exit = terminal(grade)
    require(isinstance(registry.get('registrations'), dict) and isinstance(registry.get('retirements'), dict)
            and all(isinstance(row, dict) for row in registry['registrations'].values()),
            'Unexpected I24 registry metadata')
    writers = [(identity, row) for identity, row in registry['registrations'].items()
               if row.get('role') in ('worker', 'model-proxy')]
    require(sorted(row.get('role') for _, row in writers) == ['model-proxy', 'worker'],
            'Unexpected or missing I24 budget writers')
    exits = {}
    for identity, registered in writers:
        role = registered['role']
        suffix = 'main-a-worker-a' if role == 'worker' else 'main-proxy-a'
        image = plan['pins']['worker_image_id' if role == 'worker' else 'services_image_id']
        arguments = dict(identity=identity, role=role, image=image,
                         name='harness-lab-' + trial + '-' + suffix, trial=trial)
        exact_container(registered, **arguments)
        if role == 'model-proxy':
            exact_mount(registered, '/budget', (root / expected).parent, writable=True)
        else:
            exact_mount(registered, '/config/model-run.token', (root / expected).with_name('run.token'), writable=False)
        retirement = registry['retirements'][identity]
        require(isinstance(retirement, dict) and isinstance(retirement.get('deleted'), dict),
                'Unexpected I24 retirement metadata')
        stopped, deleted = retirement['after_stop'], retirement['deleted']
        exact_container(stopped, **arguments)
        require(deleted.get('id') == identity and deleted.get('id_absent') is True
                and isinstance(deleted.get('observed_at'), str), 'I24 writer deletion is unproven')
        exits[role] = terminal(stopped)
    return {'original_verdict_passed': verdict['passed'], 'original_host_passed': host['passed'],
            'grade_exit_code': grade_exit, 'writer_exit_codes': exits, 'budget_writers_closed': True,
            'cleanup_independently_proven': False,
            'claim': 'closed spend; no task success, exit health or whole-trial cleanup inferred'}


def i25_closure(root, run, entry, sources):
    plan, tasks, host, cleanup, final, grade = (sources[name] for name in (
        'protected/plan.json', 'protected/tasks.json', 'protected/flow-host.json',
        'protected/flow-cleanup.json', 'protected/final-containers.json', 'protected/grade-flow-container.json'))
    trial = entry['run_id']
    require(plan.get('schema') == 'harness-lab-instance-cost/1'
            and plan.get('flow_schema') == 'harness-lab-instance-cost-flow/1'
            and plan.get('candidate') == entry['candidate'] == 'deepagents_langgraph'
            and plan.get('trial_id') == trial and entry['case'] == entry['budget_group'] == 'instance-cost'
            and type(plan.get('baseline_only')) is bool and entry['upstream_kind'] == 'remote',
            'I25 flow identity is unproven')
    verdict_name = 'protected/baseline-verdict.json' if plan['baseline_only'] else 'protected/flow-verdict.json'
    verdict = sources[verdict_name]
    require(verdict.get('schema') == plan['flow_schema'] and verdict.get('candidate') == entry['candidate']
            and all(verdict.get(key) == plan.get(key) for key in ('trial_id', 'pair_id', 'strategy', 'seed', 'users'))
            and type(verdict.get('cost_subset_passed')) is bool and verdict.get('i25_complete') is False
            and isinstance(host.get('task_timings'), list) and isinstance(host.get('normal_closes'), list)
            and cleanup.get('unconfirmed_resources') == [] and final == [], 'I25 execution/cleanup is unproven')
    require(isinstance(tasks, list) and all(isinstance(task, dict) for task in tasks),
            'Unexpected I25 task metadata')
    selected = [task for task in tasks if task.get('task_id') == entry['task_id']]
    require(len(selected) == 1, 'I25 task identity is ambiguous')
    task = selected[0]
    owner, kind = task['owner'], task['kind']
    require(isinstance(owner, str) and re.fullmatch(r'user-[0-9]{3}', owner)
            and owner in plan['active_users'] and kind in ('baseline', 'active', 'wake', 'fault')
            and task['task_id'] == kind + '-' + owner
            and task.get('scope') == {'experiment_id': 'instance-cost', 'variant': trial + '-' + task['task_id'],
                                     'user_id': owner, 'session_id': task['task_id']}
            and (not plan['baseline_only'] or (kind == 'baseline' and owner == plan['active_users'][0])),
            'I25 task scope is unproven')
    expected = entry['run_path'] + f'/users/{owner}/budgets/{kind}/calls.sqlite'
    require(entry['budget_path'] == expected, 'I25 budget ownership is unproven')
    created = sources[entry['proxy_created_source']]
    registered, pin = created['observed'], created['pin']
    retired = sources[entry['proxy_retired_source']]
    require(isinstance(registered, dict) and isinstance(pin, dict), 'Unexpected I25 original proxy metadata')
    identity = registered.get('id')
    arguments = dict(identity=identity, role='model-proxy', image=plan['services_image_id'],
                     name=f'hl-ic-{trial}-{kind}-proxy-{owner}')
    exact_container(registered, **arguments)
    exact_container(retired, **arguments)
    require(pin.get('id') == identity and pin.get('role') == 'model-proxy'
            and pin.get('image_id') == plan['services_image_id'] and pin.get('running') is False
            and isinstance(registered.get('state'), dict)
            and registered.get('state', {}).get('Status') == 'created'
            and registered.get('state', {}).get('Running') is False, 'I25 original proxy is unproven')
    for container in (registered, retired):
        exact_mount(container, '/budget', (root / expected).parent, writable=True)
    proxy_exit = terminal(retired)
    require(re.fullmatch(r'/hl-ic-' + re.escape(trial) + r'-bench-[1-9][0-9]*', grade.get('name', '')),
            'I25 grade container name changed')
    exact_container(grade, identity=grade.get('id'), role='bench', image=plan['bench_image_id'], name=grade['name'][1:])
    return {'original_verdict_passed': verdict['cost_subset_passed'], 'grade_exit_code': terminal(grade),
            'proxy_exit_code': proxy_exit, 'budget_writers_closed': True, 'cleanup_independently_proven': True,
            'claim': 'closed spend; no task success, exit health, isolation or complete I25 result inferred'}


def closure(root, entry):
    """Verify terminal metadata; failures remain spend, not successful tasks."""
    run = path_in(root, entry['run_path'])
    require(run.name == entry['run_id'] and run.parent.name == entry['case']
            and run.parent.parent.name == entry['candidate'], 'Run directory identity mismatch')
    sources = {}
    allowed = metadata_names(entry)
    task_sources = ({run.parent / binding['variant'] / 'chain.json': f'tasks/{task}/chain.json'
                     for task, binding in scope_task_bindings(run).items()}
                    if entry['closure_kind'] == I24_SCOPE_CLOSURE else {})
    for source in entry['closure_sources']:
        path = path_in(root, source['path'])
        if path in task_sources:
            name = task_sources[path]
        else:
            require(path.is_relative_to(run), 'Unexpected closure metadata path')
            name = path.relative_to(run).as_posix()
        require(name in allowed and name not in sources, 'Unexpected or duplicate closure metadata path')
        document, actual = load_json(path, allow_list=entry['closure_kind'] == I25_CLOSURE
                                     and name in ('protected/tasks.json', 'protected/final-containers.json'))
        require(actual == source['sha256'], 'Frozen closure metadata changed')
        sources[name] = document
    kind = entry['closure_kind']
    if kind == I24_CLOSURE:
        return i24_closure(root, run, entry, sources)
    if kind == I24_SCOPE_CLOSURE:
        return i24_scope_closure(root, run, entry, sources)
    if kind == I25_CLOSURE:
        return i25_closure(root, run, entry, sources)
    if kind == 'candidate-and-grade-terminal-exit-codes':
        manifest, verdict = sources['manifest.json'], sources['verdict.json']
        require(manifest.get('run_id') == entry['run_id'] and manifest.get('candidate') == entry['candidate']
                and type(manifest.get('candidate_exit_code')) is int and type(manifest.get('grade_exit_code')) is int
                and type(verdict.get('passed')) is bool
                and manifest.get('real_model_credential_owner') == 'model-proxy only'
                and entry['upstream_kind'] == 'remote', 'Candidate completion/remote route is unproven')
        return {'candidate_exit_code': manifest['candidate_exit_code'], 'grade_exit_code': manifest['grade_exit_code'],
                'original_verdict_passed': verdict['passed'], 'cleanup_independently_proven': False}
    if kind == 'finished-mock-probe-manifest-and-verdict':
        manifest, verdict = sources['budget-probe-manifest.json'], sources['budget-probe-verdict.json']
        require(manifest.get('run_id') == entry['run_id'] and manifest.get('candidate') == entry['candidate']
                and manifest.get('real_model_called') is False and manifest.get('mock_only_internal_network') is True
                and verdict.get('real_model_called') is False and type(verdict.get('passed')) is bool
                and manifest.get('rounds') and all(type(row.get('exit_code')) is int
                    and type(row.get('command_exit')) is int for row in manifest['rounds'])
                and entry['upstream_kind'] == 'mock', 'Mock probe completion/route is unproven')
        return {'real_model_called': False, 'mock_only_internal_network': True,
                'rounds': len(manifest['rounds']), 'original_verdict_passed': verdict['passed'],
                'error_count': len(manifest.get('errors', [])), 'cleanup_independently_proven': False}
    if kind == 'finished-orchestration-and-cleanup':
        manifest, cleanup = sources['orchestration.json'], sources['cleanup.json']
        require(manifest.get('chain_id') == entry['run_id'] and manifest.get('candidate') == entry['candidate']
                and isinstance(manifest.get('finished_at'), str) and cleanup.get('passed') is True
                and cleanup.get('remaining') == [] and isinstance(cleanup.get('finished_at'), str)
                and entry['upstream_kind'] == 'remote', 'Pause chain closure is unproven')
        return {'finished_at': manifest['finished_at'], 'error_count': len(manifest.get('errors', [])),
                'original_verdict_passed': sources.get('verdict.json', {}).get('passed'),
                'cleanup_independently_proven': True}
    if kind == 'finished-fault-chain-and-cleanup':
        chain, manifest, cleanup = (sources[name] for name in ('chain.json', 'orchestration.json', 'cleanup.json'))
        scope = {'experiment_id': 'fault-recovery', 'variant': entry['run_id'], 'user_id': 'alice', 'session_id': 'main'}
        require(chain.get('schema') == 'harness-lab-fault-chain/1'
                and manifest.get('schema') == 'harness-lab-fault-orchestration/1'
                and chain.get('candidate') == entry['candidate']
                and chain.get('chain_id') == manifest.get('chain_id') == entry['run_id']
                and chain.get('scope') == manifest.get('scope') == scope
                and entry['case'] == entry['budget_group'] == 'fault-recovery'
                and entry['budget_path'] == f"budgets/fault-recovery/{entry['run_id']}/{entry['run_id']}/calls.sqlite"
                and isinstance(manifest.get('finished_at'), str)
                and isinstance(manifest.get('errors'), list)
                and cleanup.get('remaining') == [] and isinstance(cleanup.get('finished_at'), str)
                and entry['upstream_kind'] == 'remote', 'Fault chain closure or ownership is unproven')
        return {'finished_at': manifest['finished_at'], 'error_count': len(manifest['errors']),
                'original_verdict_passed': sources.get('verdict.json', {}).get('passed'),
                'cleanup_independently_proven': True, 'claim': 'closed spend; no recovery or task success inferred'}
    if kind == 'host-finished-and-cleanup-or-followup':
        host = sources['host.json']
        require(host.get('model_called') is False and host.get('remote_key_used') is False
                and (host.get('cleanup') is True or sources.get('cleanup-followup.json', {}).get('removed') is True)
                and entry['upstream_kind'] == 'no-model', 'No-model calibration closure is unproven')
        return {'model_called': False, 'remote_key_used': False, 'original_host_passed': host.get('passed'),
                'cleanup_independently_proven': True, 'cleanup_followup_used': host.get('cleanup') is not True}
    raise ValueError('Unknown closure rule')


def calls(database, upstream):
    # Deliberately never SELECT the budget table or read run.token.
    with sqlite3.connect(database.as_uri() + '?mode=ro', uri=True, timeout=2) as connection:
        connection.execute('PRAGMA query_only=ON')
        connection.execute('BEGIN')
        rows = [dict(zip(COLUMNS, row)) for row in connection.execute(
            'SELECT id,started_at,path,outcome,upstream_status FROM calls ORDER BY id').fetchall()]
    require([row['id'] for row in rows] == list(range(1, len(rows) + 1)), 'Reservation IDs are not contiguous')
    counters = Counter({key: 0 for key in BUCKETS})
    for row in rows:
        outcome, status = row['outcome'], row['upstream_status']
        if outcome == 'forwarded' and type(status) is int and 100 <= status <= 599:
            if upstream == 'remote':
                bucket = 'remote_forwarded_2xx' if 200 <= status < 300 else 'remote_forwarded_non2xx'
            elif upstream == 'mock':
                bucket = 'mock_forwarded'
            else:
                bucket = 'unknown_or_inflight'
        elif outcome == 'injected_timeout':
            bucket = 'local_injected'
        elif outcome in ('rejected', 'denied'):
            bucket = 'recorded_rejected'
        elif outcome in ('upstream_timeout', 'upstream_error', 'client_disconnected') and upstream == 'remote':
            bucket = 'remote_attempt_unconfirmed'
        else:
            bucket = 'unknown_or_inflight'
        counters[bucket] += 1
    require(sum(counters.values()) == len(rows), 'Reservation categories do not partition the ledger')
    encoded = json.dumps(rows, sort_keys=True, separators=(',', ':')).encode()
    return {'reservations': len(rows), **counters,
            'remote_forwarded': counters['remote_forwarded_2xx'] + counters['remote_forwarded_non2xx'],
            'outcomes': dict(sorted(Counter(row['outcome'] for row in rows).items())),
            'upstream_statuses': dict(sorted(Counter(str(row['upstream_status']) for row in rows).items())),
            'selected_call_metadata_sha256': sha(encoded)}


def discover(root, date):
    """Paths only: an unlisted/active database is not opened or counted."""
    found = set()
    for base in (root / 'budgets', root / 'runs' / date):
        for directory, children, filenames in os.walk(base, followlinks=False):
            children[:] = sorted(name for name in children if name != 'old' and not (Path(directory) / name).is_symlink())
            if 'calls.sqlite' in filenames:
                found.add((Path(directory) / 'calls.sqlite').relative_to(root).as_posix())
    return found


def aggregate(rows, keys):
    groups = defaultdict(list)
    for row in rows:
        groups[tuple(row[key] for key in keys)].append(row)
    return [{**dict(zip(keys, group)), 'databases': len(members),
             **{key: sum(row['usage'][key] for row in members) for key in ('reservations', 'remote_forwarded', *BUCKETS)}}
            for group, members in sorted(groups.items())]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, default=Path('docs/validation/research-usage-inventory.json'))
    parser.add_argument('--output', type=Path)
    args = parser.parse_args()
    inventory, inventory_hash = load_json(args.inventory)
    require(inventory.get('schema') == 'harness-lab-research-usage-inventory/1', 'Unexpected inventory schema')
    root = Path(inventory['data_root']).resolve(strict=True)
    rows, unresolved, aliases = [], list(inventory['unresolved_databases']), []
    enrolled, identities = set(), {}
    for entry in inventory['entries']:
        relative = entry['budget_path']
        require(relative not in enrolled, 'Same database enrolled twice')
        enrolled.add(relative)
        try:
            database = path_in(root, relative)
            require(database.name == 'calls.sqlite' and database.is_file(), 'Budget database is missing')
            facts = closure(root, entry)
            stat = database.stat()
            identity = (stat.st_dev, stat.st_ino)
            if identity in identities:
                aliases.append({'budget_path': relative, 'same_physical_database_as': identities[identity]})
                continue
            usage = calls(database, entry['upstream_kind'])
            require(entry['upstream_kind'] != 'no-model' or usage['reservations'] == 0,
                    'No-model calibration unexpectedly contains reservations')
            identities[identity] = relative
            rows.append({**entry, 'database_path_absolute': str(database), 'closure_facts': facts, 'usage': usage})
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            # Do not print exception text, arbitrary metadata or credential paths.
            unresolved.append({'budget_path': relative, 'run_id': entry['run_id'],
                               'reason': 'Listed closure/ownership/metadata/SQL validation failed; excluded from totals'})
    discovered = discover(root, inventory['date'])
    already_excluded = {row['budget_path'] for row in unresolved}
    unlisted = [{'budget_path': relative, 'reason': 'Not in the frozen enrollment; database not queried'}
                for relative in sorted(discovered - enrolled - already_excluded)]
    totals = aggregate(rows, ())
    result = {'schema': 'harness-lab-research-usage-summary/1', 'date': inventory['date'], 'cutoff': inventory['cutoff'],
              'inventory': {'path': str(args.inventory.resolve()), 'sha256': inventory_hash},
              'claim': 'Closed, attributable surviving databases only; not a complete research bill or success-rate comparison',
              'selected_sql_columns': list(COLUMNS), 'closed_databases': rows,
              'by_candidate': aggregate(rows, ('candidate',)), 'by_research_category': aggregate(rows, ('research_category',)),
              'closed_scope_totals': totals[0] if totals else {}, 'deduplicated_aliases': aliases,
              'unresolved_databases': unresolved, 'unlisted_databases': unlisted,
              'missing_budget_runs': inventory['missing_budget_runs'],
              'unknown': ['model token usage and billed amount', 'human effort and full infrastructure cost',
                          'requests rejected before reservation (e.g. auth failures/call-cap 429); not recorded in calls table',
                          *inventory['known_out_of_scope']]}
    encoded = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
    if args.output:
        destination = args.output.resolve()
        require(not destination.exists() and root not in destination.parents and destination != root,
                'Refuse to overwrite evidence or an existing snapshot')
        with destination.open('x') as target:
            target.write(encoded)
    else:
        print(encoded, end='')


if __name__ == '__main__':
    main()
