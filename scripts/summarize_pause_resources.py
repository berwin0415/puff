"""汇总明确选定的 I22 原始样本；不重新评分。

仅聚合宿主侧证据：不执行候选、工具、模型或 Docker API。样本均值不是时间积分、计费数据或生产节省比例。
"""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime
import hashlib
import json
from pathlib import Path
import statistics
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from runner.metrics import memory_bytes

CHAINS = {
    'deepagents_langgraph': (
        'i22-deepagents-pause-seed-101-004',
        'i22-deepagents-pause-seed-202-001',
        'i22-deepagents-pause-seed-303-001',
    ),
    'deepseek_harness': (
        'i22-deepseek-pause-seed-101-002',
        'i22-deepseek-pause-seed-202-001',
        'i22-deepseek-pause-seed-303-001',
    ),
}


def regular(path):
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError('证据必须是已存在的普通文件')
    return path.read_bytes()


def totals(rows):
    return {'memory_mib': sum(memory_bytes(r['MemUsage'].split('/')[0]) for r in rows) / 1024**2,
            'cpu_percent': sum(float(r['CPUPerc'].rstrip('%')) for r in rows),
            'pids_including_threads': sum(int(r['PIDs']) for r in rows), 'containers': len(rows)}


def aggregate(values):
    return {k: {'sample_mean': statistics.mean(v[k] for v in values),
                'peak': max(v[k] for v in values)} for k in values[0]} if values else {}


def summarize(data_root, day):
    runs = []
    for candidate, chains in CHAINS.items():
        for chain in chains:
            directory = data_root / 'runs' / day / candidate / 'tools' / chain
            sources = {}
            for filename in ('resource-samples.jsonl', 'cleanup.json', 'orchestration.json',
                             'trusted/worker-a.json', 'trusted/worker-b.json'):
                data = regular(directory / filename)
                sources[filename] = {'sha256': hashlib.sha256(data).hexdigest(), 'data': data}
            cleanup = json.loads(sources['cleanup.json']['data'])
            if cleanup.get('passed') is not True or cleanup.get('remaining') != []:
                raise ValueError('仅可汇总已结束且清理成功的链')
            orchestration = json.loads(sources['orchestration.json']['data'])
            rows = [json.loads(line) for line in sources['resource-samples.jsonl']['data'].splitlines() if line]
            grouped = defaultdict(list)
            for row in rows:
                grouped[row['phase']].append(row)
            phases = {}
            for phase, samples in grouped.items():
                valid = [s for s in samples if s.get('error') is None]
                phases[phase] = {
                    'samples': len(samples), 'valid_samples': len(valid),
                    'sample_errors': len(samples) - len(valid),
                    'observed_span_seconds': max(s['unix_time'] for s in samples) - min(s['unix_time'] for s in samples),
                    'all_containers': aggregate([totals(s['containers']) for s in valid]),
                    'workers': aggregate([totals([r for r in s['containers'] if r['role'] == 'worker']) for s in valid]),
                    'shared_services': aggregate([totals([r for r in s['containers'] if r['role'] == 'shared_service']) for s in valid]),
                    'bench_and_monitor': aggregate([totals([r for r in s['containers'] if r['role'] == 'bench']) for s in valid]),
                }
            worker_a = json.loads(sources['trusted/worker-a.json']['data'])
            worker_b = json.loads(sources['trusted/worker-b.json']['data'])
            def elapsed(end, begin):
                return (datetime.fromisoformat(end) - datetime.fromisoformat(begin)).total_seconds()
            runs.append({'candidate': candidate, 'chain_id': chain,
                         'used_active_seconds': orchestration.get('used_active_seconds'),
                         'same_host_clock_timings': {
                             'signal_sent_to_a_exit_observed_seconds': elapsed(worker_a['finished_at'], worker_a['pause_request']['sent_at']),
                             'b_started_to_exit_observed_seconds': elapsed(worker_b['finished_at'], worker_b['started_at']),
                             'note': '包含宿主轮询和检查；不推算首次原生恢复活动时间或确切安全点延迟。'},
                         'phases': phases,
                         'sources': {name: {'path': str(directory / name), 'sha256': row['sha256']}
                                     for name, row in sources.items()}})
    return {'schema': 'harness-lab-pause-resource-summary/1', 'date': day, 'runs': runs,
            'note': '仅描述原始样本，不重新评分。均值按样本计算，不按时间加权。暂停资源包含实验服务和监控器。不据此推断金额或生产节省比例。'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-root', type=Path, required=True)
    parser.add_argument('--date', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.data_root.resolve(), args.date)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({'chains': len(result['runs']), 'output': str(args.output.resolve())}))


if __name__ == '__main__':
    main()
