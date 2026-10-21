"""Summarize raw Docker measurements; unknown model prices stay unknown."""
from __future__ import annotations

import re
import statistics


def memory_bytes(value: str) -> float:
    match = re.fullmatch(r'([\d.]+)\s*(B|KiB|MiB|GiB|kB|MB|GB)', value.strip())
    if not match:
        raise ValueError('Unknown Docker memory unit')
    multipliers = {'B': 1, 'KiB': 1024, 'MiB': 1024**2, 'GiB': 1024**3,
                   'kB': 1000, 'MB': 1000**2, 'GB': 1000**3}
    return float(match[1]) * multipliers[match[2]]


def summarize(samples: list[dict]) -> dict:
    totals = []
    for sample in samples:
        if 'error' in sample:
            continue
        rows = sample.get('containers', [])
        totals.append({'memory_bytes': sum(memory_bytes(row['MemUsage'].split('/')[0]) for row in rows),
                       'cpu_percent': sum(float(row['CPUPerc'].rstrip('%')) for row in rows),
                       'pids': sum(int(row['PIDs']) for row in rows if row['PIDs'].isdigit()),
                       'container_count': len(rows)})
    return {'samples': len(totals), 'sample_errors': sum('error' in sample for sample in samples),
            'resources': {key: {'mean': statistics.mean(t[key] for t in totals),
                                 'peak': max(t[key] for t in totals)}
                          for key in ('memory_bytes', 'cpu_percent', 'pids', 'container_count')} if totals else {},
            'model_cost_usd': None, 'price_source': None,
            'note': 'Docker CPU is percent of one CPU; PIDs includes threads. No candidate result is inferred.'}
