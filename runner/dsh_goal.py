"""Independent durable replay of the pinned DSH Goal format, without its runtime.

Source: packages/goal/goal/src/{fold,types,index}.ts in the frozen DSH export.
GoalSnapshot is nested; counters/timestamps are outside it. Activation belongs
only to the live GoalView and MUST NOT be reconstructed from durable phase.
"""
from __future__ import annotations

import re


def need(value, code):
    if not value:
        raise ValueError('dsh_goal_' + code)


def integer(value, *, positive=False):
    return type(value) is int and (1 if positive else 0) <= value <= 2**53 - 1


def snapshot(value):
    need(isinstance(value, dict), 'snapshot_required')
    blocked = value.get('phase') == 'blocked'
    fields = {'id', 'revision', 'objective', 'phase', 'maxGoalRounds'} | ({'blockedReason'} if blocked else set())
    need(set(value) == fields, 'snapshot_fields')
    need(isinstance(value['id'], str) and value['id'] and integer(value['revision'], positive=True), 'identity')
    need(isinstance(value['objective'], str) and value['objective'].strip()
         and value['objective'] == value['objective'].strip(), 'objective')
    need(value['phase'] in {'active', 'paused', 'blocked', 'complete'}
         and integer(value['maxGoalRounds'], positive=True), 'phase_or_cap')
    if blocked:
        reason = value['blockedReason']
        need(isinstance(reason, dict) and set(reason) == {'code', 'message'}
             and isinstance(reason['code'], str) and re.fullmatch(r'[a-z][a-z0-9]*(?:-[a-z0-9]+)*', reason['code'])
             and isinstance(reason['message'], str) and reason['message'].strip()
             and reason['message'] == reason['message'].strip(), 'block_reason')
    return dict(value)


def change_projection(data):
    """Decode a non-clear durable change, without inventing live activation."""
    need(isinstance(data, dict) and set(data) == {'kind', 'version', 'operation', 'goal', 'roundsStarted', 'createdAt', 'updatedAt'}
         and data['kind'] == 'goal/change' and type(data['version']) is int and data['version'] == 1
         and data['operation'] in {'create', 'edit', 'pause', 'resume', 'complete', 'block'}, 'change_fields')
    goal = snapshot(data['goal'])
    need(all(integer(data[key]) for key in ('roundsStarted', 'createdAt', 'updatedAt'))
         and data['updatedAt'] >= data['createdAt'], 'counters_or_timestamps')
    return {**goal, **{key: data[key] for key in ('roundsStarted', 'createdAt', 'updatedAt')}}


def matches_live_view(view, durable, activation):
    """Compare a reported public GoalView to independently folded durable data.

    The caller supplies activation only with the matching official API/event
    semantics; this function does not infer armed/disarmed from phase.
    """
    return isinstance(view, dict) and durable is not None and view == {**durable, 'activation': activation}


def fold_durable_goal(events):
    """Mirror the official strict fold. Return flattened durable fields or None."""
    current, seen = None, set()
    for event in events:
        if event.get('type') == 'user/message':
            source = event.get('data', {}).get('source', {})
            if not isinstance(source, dict) or source.get('kind') != 'goal':
                continue
            need(current is not None and current['phase'] == 'active'
                 and source.get('goalId') == current['id'] and source.get('revision') == current['revision']
                 and integer(source.get('revision'), positive=True) and integer(source.get('round'), positive=True)
                 and source['round'] == current['roundsStarted'] + 1 and source['round'] <= current['maxGoalRounds'], 'round_not_next_admitted')
            current = {**current, 'roundsStarted': source['round']}
            continue
        if event.get('type') != 'goal/change':
            continue
        data = event.get('data')
        need(isinstance(data, dict), 'change_required')
        operation = data.get('operation')
        if operation == 'clear':
            need(set(data) == {'kind', 'version', 'operation', 'cleared', 'clearedAt'} and data['kind'] == 'goal/change'
                 and type(data['version']) is int and data['version'] == 1, 'clear_fields')
            ref = data['cleared']
            need(current is not None and isinstance(ref, dict) and set(ref) == {'id', 'revision'}
                 and ref['id'] == current['id'] and integer(ref['revision'], positive=True)
                 and ref['revision'] == current['revision'] + 1 and integer(data['clearedAt'])
                 and data['clearedAt'] >= current['updatedAt'], 'clear_transition')
            current = None
            continue
        next_goal = change_projection(data)
        if operation == 'create':
            need(next_goal['revision'] == 1 and next_goal['phase'] == 'active' and next_goal['roundsStarted'] == 0
                 and (current is None or current['phase'] == 'complete') and next_goal['id'] not in seen, 'create_transition')
            seen.add(next_goal['id'])
        else:
            need(current is not None and next_goal['id'] == current['id']
                 and next_goal['revision'] == current['revision'] + 1, 'revision_transition')
            need(next_goal['roundsStarted'] == current['roundsStarted'] and next_goal['createdAt'] == current['createdAt']
                 and next_goal['updatedAt'] >= current['updatedAt'], 'counter_transition')
            if operation == 'edit':
                need(next_goal['phase'] == current['phase'] and next_goal.get('blockedReason') == current.get('blockedReason'), 'edit_transition')
            else:
                need(next_goal['objective'] == current['objective'] and next_goal['maxGoalRounds'] == current['maxGoalRounds'], 'definition_changed')
                allowed = {'pause': ({'active'}, 'paused'), 'resume': ({'active', 'paused', 'blocked'}, 'active'),
                           'complete': ({'active', 'paused', 'blocked'}, 'complete'), 'block': ({'active'}, 'blocked')}
                before, after = allowed[operation]
                need(current['phase'] in before and next_goal['phase'] == after, 'phase_transition')
                if operation == 'resume':
                    need(current['roundsStarted'] < next_goal['maxGoalRounds'], 'resume_round_budget')
        current = next_goal
    return current
