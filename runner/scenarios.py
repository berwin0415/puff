"""Load the unified T1-T6 task configs and fixture scenarios.

The task configs describe the shared acceptance rules; the scenario files
describe the deterministic fixture steps used to exercise them.  Keeping the
lookup here means the runner, scripts and tests read the same definitions.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import yaml


REPO_ROOT = Path(__file__).resolve().parents[1]
TASKS_DIR = REPO_ROOT / "configs" / "tasks"
SCENARIOS_DIR = REPO_ROOT / "fixtures" / "scenarios"


def load_task_config(task_id: str, root: str | Path | None = None) -> dict[str, Any]:
    """Return the YAML task config whose ``task_id`` matches *task_id*."""

    directory = Path(root) if root is not None else TASKS_DIR
    path = directory / f"{task_id}.yaml"
    if path.exists():
        value = yaml.safe_load(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError(f"task config must be a mapping: {path}")
        return value
    for candidate in sorted(directory.glob("*.yaml")):
        value = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("task_id") == task_id:
            return value
    raise FileNotFoundError(f"no task config for {task_id!r} under {directory}")


def load_scenario(task_id: str, root: str | Path | None = None) -> dict[str, Any]:
    """Return the JSON scenario linked to *task_id* through ``config_task_id``."""

    directory = Path(root) if root is not None else SCENARIOS_DIR
    for candidate in sorted(directory.glob("*.json")):
        value = json.loads(candidate.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("config_task_id") == task_id:
            return value
    raise FileNotFoundError(f"no fixture scenario for {task_id!r} under {directory}")


def task_ids(root: str | Path | None = None) -> list[str]:
    directory = Path(root) if root is not None else TASKS_DIR
    values = []
    for candidate in sorted(directory.glob("*.yaml")):
        value = yaml.safe_load(candidate.read_text(encoding="utf-8"))
        if isinstance(value, dict) and value.get("task_id"):
            values.append(str(value["task_id"]))
    return values


__all__ = ["REPO_ROOT", "SCENARIOS_DIR", "TASKS_DIR", "load_scenario", "load_task_config", "task_ids"]
