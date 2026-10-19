from __future__ import annotations

from pathlib import Path


def test_compose_file_declares_shared_topology():
    compose = Path(__file__).parents[2] / "compose" / "docker-compose.yml"
    text = compose.read_text(encoding="utf-8")
    for service in ("fixtures:", "execution:", "runner:", "adapter:"):
        assert service in text
    assert "condition: service_healthy" in text
