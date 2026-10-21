import pytest

from runner.metrics import memory_bytes, summarize


def test_resource_summary_sums_shared_services_without_turning_missing_samples_into_zero():
    row = lambda memory, cpu, pids: {'MemUsage': memory, 'CPUPerc': cpu, 'PIDs': pids}
    result = summarize([
        {'containers': [row('1GiB / 2GiB', '110%', '5'), row('512MiB / 1GiB', '20%', '3')]},
        {'error': 'sampling failed', 'containers': []},
        {'containers': [row('512MiB / 1GiB', '10%', '4')]},
    ])
    assert result['samples'] == 2 and result['sample_errors'] == 1
    assert result['resources']['memory_bytes'] == {'mean': 1024**3, 'peak': 1.5 * 1024**3}
    assert result['resources']['cpu_percent'] == {'mean': 70, 'peak': 130}
    assert result['resources']['container_count'] == {'mean': 1.5, 'peak': 2}
    assert result['model_cost_usd'] is None


def test_memory_units_are_not_mixed_with_container_counts():
    assert memory_bytes('1MB') == 1_000_000
    assert memory_bytes('1MiB') == 1_048_576
    with pytest.raises(ValueError):
        memory_bytes('unknown')
    assert summarize([])['resources'] == {}
