"""Append an offline review after correcting I22 DSH durable-Goal grading.

Runs in the pinned bench image. It never alters the original verdict, controls,
state, tools audit or ledger and never calls a candidate, service or model.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from runner.bench import environment, identifier
from runner import detailed
from runner.grading import grade_tools
from runner import pause_recovery as pause
from runner import dsh_goal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', choices=('deepseek_harness',), required=True)
    parser.add_argument('--chain-id', type=identifier, required=True)
    parser.add_argument('--seed', type=int, choices=(101, 202, 303), required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--review-id', type=identifier, required=True)
    args = parser.parse_args()
    pause.require(environment()['model_credentials_injected'] is False, 'review_must_not_receive_credentials')
    config, paths, scope = pause.context(args)
    output = paths.run / 'reviews' / args.review_id
    pause.require(output.is_dir() and not list(output.iterdir()), 'fresh_host_review_mount_required')
    checks, metrics = [], {}

    def check(name, callback):
        try:
            value = callback()
            checks.append({'name': name, 'passed': True})
            return value
        except Exception as error:
            checks.append({'name': name, 'passed': False, 'error': type(error).__name__ + ':' + str(error)[:160]})

    pins = check('frozen_chain_identity', lambda: pause.pinned(args, paths, scope))
    if pins is not None and pins['variant'] == 'requested-pause':
        original = paths.run / 'verdict.json'
        provenance = {'schema': 'harness-lab-pause-review-provenance/1',
            'reason': 'Pinned JSONL formatter materializes delegationDepth=0; original grader compared in-memory and restored headers without the official default.',
            'original_verdict_sha256': pause.sha(pause.regular(original)),
            'original_ledger_sha256': pause.sha(pause.regular(paths.run / 'trusted/final-ledger.json')),
            'grader_sha256': pause.sha(Path(pause.__file__).read_bytes()),
            'review_program_sha256': pause.sha(Path(__file__).read_bytes()),
            'model_or_candidate_executed': False, 'original_evidence_modified': False}
        passed = pause.grade(args, config, paths, scope, review_output=output)
        pause.require(pause.sha(pause.regular(original)) == provenance['original_verdict_sha256'],
                      'original_verdict_changed_during_review')
        pause.create(output / 'review-provenance.json', provenance)
        print(json.dumps({'chain_id': args.chain_id, 'passed': passed, 'mode': 'offline_full_chain_review'}))
        return 0 if passed else 1
    if pins is not None:
        check('original_uninterrupted_variant', lambda: pause.require(pins['variant'] == 'uninterrupted', 'not_an_uninterrupted_baseline'))
        check('original_worker_process', lambda: pause.host_record(args, config, paths, scope, pins, 'worker-a', paused=False))
        check('original_protected_controls', lambda: pause.control_integrity(paths, 'worker-a'))
        check('native_completion_with_durable_goal_projection', lambda: pause.final_native(paths, scope, pins, 'worker-a'))
        budget = check('original_model_budget', lambda: pause.budget_snapshot(paths.budget))
        if budget is not None:
            metrics['model_calls'] = budget['count']
        check('original_trusted_tool_audit', lambda: pause.audit_snapshot(paths.run / 'tool-events.jsonl', scope))

        def business():
            ledger = pause.read(paths.run / 'trusted/final-ledger.json')
            rows = [row for _, _, row in pause.jsonl(paths.run / 'tool-events.jsonl')]
            task, _ = detailed.task_definition('tools')
            verdict = grade_tools(args.seed, scope, paths.workspace, ledger, rows, task['workspace_inputs']['request.json'])
            pause.create(output / 'business-verdict.json', verdict)
            metrics.update(verdict.get('metrics', {}))
            pause.require(verdict['passed'], 'original_independent_business_result_failed')
        check('business_recomputed_from_original_frozen_ledger', business)
    original = paths.run / 'verdict.json'
    verdict = {'schema': 'harness-lab-pause-baseline-review/1', 'chain_id': args.chain_id,
        'candidate': args.candidate, 'seed': args.seed, 'review_id': args.review_id,
        'reason': 'Original grader confused persisted GoalSpec with GoalView; activation is process-local and not in the durable event.',
        'original_verdict_sha256': hashlib.sha256(pause.regular(original)).hexdigest(),
        'grader_sha256': hashlib.sha256(Path(pause.__file__).read_bytes()).hexdigest(),
        'durable_goal_grader_sha256': hashlib.sha256(Path(dsh_goal.__file__).read_bytes()).hexdigest(),
        'review_program_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'checks': checks, 'metrics': metrics, 'passed': bool(checks) and all(row['passed'] for row in checks),
        'model_or_candidate_executed': False, 'original_evidence_modified': False}
    pause.create(output / 'verdict.json', verdict)
    print(json.dumps({'chain_id': args.chain_id, 'passed': verdict['passed'], 'metrics': metrics}))
    return 0 if verdict['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
