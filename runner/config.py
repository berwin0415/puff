"""Strict experiment settings and paths; this module never reads credentials.

The host and containers use the same relative layout. Call
``config.for_environment("container")`` inside a container mounted at ``/data``.
Only a resume chain may reuse its experiment, variant, user and session IDs;
independent seeds/repetitions must receive distinct experiment or variant IDs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date as Date, datetime
from pathlib import Path
import math
import re
from typing import Any, Literal
from zoneinfo import ZoneInfo

import yaml


DEFAULT_CONFIG_PATH = Path(__file__).resolve().parents[1] / "configs/experiment.yaml"
_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LAYOUT = ("state", "workspaces", "fixtures", "budgets", "runs")


class ConfigError(ValueError):
    """Invalid settings or an unsafe experiment path."""


class _UniqueKeyLoader(yaml.SafeLoader):
    """Reject duplicate YAML keys instead of silently retaining the last one."""


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ConfigError("Configuration mapping keys must be strings")
        if key in mapping:
            raise ConfigError(f"Duplicate configuration key: {key}")
        mapping[key] = loader.construct_object(value_node, deep=True)
    return mapping


_UniqueKeyLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _unique_mapping)


@dataclass(frozen=True)
class ModelConfig:
    api_base_url_env: str
    api_key_env: str
    name_env: str


@dataclass(frozen=True)
class BudgetConfig:
    active_timeout_seconds: int
    max_model_calls: int
    max_retries: int
    pause_wait_seconds: int
    budget_usd_reference: float
    usd_limit_enforced: bool
    price_source: str | None


@dataclass(frozen=True)
class SeedConfig:
    smoke: tuple[int, ...]
    validation: tuple[int, ...]
    final: tuple[int, ...]


@dataclass(frozen=True)
class ConcurrencyConfig:
    max_active_tasks: int
    idle_user_counts: tuple[int, ...]


@dataclass(frozen=True)
class ResourceConfig:
    memory_bytes: int
    cpus: float


@dataclass(frozen=True)
class SamplingConfig:
    interval_seconds: float


@dataclass(frozen=True)
class ExecutionPolicy:
    ordinary_operations: str
    sensitive_operations: str
    human_approval: bool


@dataclass(frozen=True)
class RunPaths:
    state: Path
    workspace: Path
    fixture: Path
    budget: Path
    run: Path


@dataclass(frozen=True)
class ExperimentConfig:
    schema_version: int
    host_data_root: Path
    container_data_root: Path
    model: ModelConfig
    budget: BudgetConfig
    seeds: SeedConfig
    concurrency: ConcurrencyConfig
    resources: ResourceConfig
    sampling: SamplingConfig
    execution_policy: ExecutionPolicy
    environment: Literal["host", "container"] = "host"

    @property
    def data_root(self) -> Path:
        return self.host_data_root if self.environment == "host" else self.container_data_root

    def for_environment(self, environment: Literal["host", "container"]) -> ExperimentConfig:
        if environment not in ("host", "container"):
            raise ConfigError("Environment must be host or container")
        return replace(self, environment=environment)

    def initialize_layout(self) -> dict[str, Path]:
        """Create only the five current experiment directories, excluding archives."""
        root = _root(self.data_root, "data_root")
        directories = {name: root / name for name in _LAYOUT}
        # Check all paths before creating anything: an existing unsafe child must
        # not leave a partially initialized layout behind.
        for directory in directories.values():
            _safe_path(root, directory)
        for directory in directories.values():
            directory.mkdir(parents=True, exist_ok=True)
            _safe_path(root, directory)
        return directories

    def paths_for(
        self,
        experiment_id: str,
        variant: str,
        candidate: str,
        user: str,
        session: str,
        run_id: str,
        case: str = "tools",
        date: str | Date | None = None,
    ) -> RunPaths:
        """Calculate paths without creating them or reading archived data.

        Reusing IDs returns the same state and budget after a container restart.
        A changed variant separates state, workspaces, fixture ledgers and budgets.
        Evidence run IDs must be unique across independent experiments.
        """
        components = {
            name: _component(value, name)
            for name, value in {
                "experiment_id": experiment_id,
                "variant": variant,
                "candidate": candidate,
                "user": user,
                "session": session,
                "run_id": run_id,
                "case": case,
            }.items()
        }
        day = _date(date)
        root = _root(self.data_root, "data_root")
        shared = (
            components["experiment_id"], components["variant"],
            components["candidate"], components["user"], components["session"],
        )
        paths = RunPaths(
            state=root.joinpath("state", *shared),
            workspace=root.joinpath("workspaces", *shared),
            fixture=root.joinpath("fixtures", components["experiment_id"], components["variant"]),
            budget=root.joinpath("budgets", components["experiment_id"], components["variant"], components["run_id"]),
            run=root.joinpath("runs", day, components["candidate"], components["case"], components["run_id"]),
        )
        for path in (paths.state, paths.workspace, paths.fixture, paths.budget, paths.run):
            _safe_path(root, path)
        return paths


def _component(value: str, name: str) -> str:
    if not isinstance(value, str) or not _COMPONENT.fullmatch(value) or value.lower() == "old":
        raise ConfigError(f"Invalid path component: {name}")
    return value


def _date(value: str | Date | None) -> str:
    if value is None:
        return datetime.now(ZoneInfo("Asia/Shanghai")).date().isoformat()
    if type(value) is Date:
        return value.isoformat()
    if isinstance(value, str):
        try:
            parsed = Date.fromisoformat(value)
        except ValueError:
            pass
        else:
            if parsed.isoformat() == value:
                return value
    raise ConfigError("date must be a date or YYYY-MM-DD")


def _no_symlinks(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ConfigError("Experiment paths must not contain symbolic links")
        if current.exists() and not current.is_dir():
            raise ConfigError("Experiment directory path is occupied by a file")


def _root(value: str | Path, name: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ConfigError(f"{name} must be an absolute path")
    path = Path(value)
    if (
        not path.is_absolute() or path == Path(path.anchor)
        or ".." in path.parts or any(part.lower() == "old" for part in path.parts)
        or "~" in str(path) or "\\" in str(path)
        or any(ord(character) < 32 for character in str(path))
    ):
        raise ConfigError(f"{name} must be an absolute non-archive directory without traversal")
    _no_symlinks(path)
    if path.exists() and not path.is_dir():
        raise ConfigError(f"{name} must be a directory")
    return path


def _safe_path(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ConfigError("Experiment path escapes its data root") from exc
    if not relative.parts or relative.parts[0] not in _LAYOUT:
        raise ConfigError("Experiment path is outside the current layout")
    _no_symlinks(path)
    if path.exists() and not path.is_dir():
        raise ConfigError("Experiment directory path is occupied by a file")


def _mapping(value: Any, name: str, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} must be a mapping")
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing or extra:
        raise ConfigError(f"{name}: missing keys {sorted(missing)}; unknown keys {sorted(extra)}")
    return value


def _integer(value: Any, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{name} must be an integer >= {minimum}")
    return value


def _number(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} must be a finite positive number")
    return float(value)


def _integers(value: Any, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} must be a nonempty integer list")
    values = tuple(_integer(item, name, minimum=0) for item in value)
    if len(set(values)) != len(values):
        raise ConfigError(f"{name} contains duplicates")
    return values


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> ExperimentConfig:
    """Read strict YAML; model fields contain names only, never secret values."""
    source = Path(path).absolute()
    if ".." in source.parts or any(part.lower() == "old" for part in source.parts):
        raise ConfigError("Configuration must not be loaded from an archive or traversal path")
    _no_symlinks(source.parent)
    if source.is_symlink():
        raise ConfigError("Configuration must not be a symbolic link")
    try:
        with source.open(encoding="utf-8") as stream:
            document = yaml.load(stream, Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("Cannot read experiment configuration") from exc
    root = _mapping(document, "configuration", {
        "schema_version", "paths", "model", "budget", "seeds", "concurrency",
        "resources", "sampling", "execution_policy",
    })
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ConfigError("Unsupported configuration schema_version")
    paths = _mapping(root["paths"], "paths", {"data_root", "container_data_root"})
    host_data_root = _root(paths["data_root"], "paths.data_root")
    container_data_root = _root(paths["container_data_root"], "paths.container_data_root")
    model = _mapping(root["model"], "model", {"api_base_url_env", "api_key_env", "name_env"})
    expected_model = {
        "api_base_url_env": "MODEL_API_BASE_URL",
        "api_key_env": "MODEL_API_KEY",
        "name_env": "MODEL_NAME",
    }
    if model != expected_model:
        raise ConfigError("Model settings must reference the three approved environment variable names")
    budget = _mapping(root["budget"], "budget", {
        "active_timeout_seconds", "max_model_calls", "max_retries", "pause_wait_seconds",
        "budget_usd_reference", "usd_limit_enforced", "price_source",
    })
    if budget["usd_limit_enforced"] is not False or budget["price_source"] is not None:
        raise ConfigError("Money enforcement is unavailable; keep usd_limit_enforced false and price_source null")
    parsed_budget = BudgetConfig(
        active_timeout_seconds=_integer(budget["active_timeout_seconds"], "budget.active_timeout_seconds"),
        max_model_calls=_integer(budget["max_model_calls"], "budget.max_model_calls"),
        max_retries=_integer(budget["max_retries"], "budget.max_retries", minimum=0),
        pause_wait_seconds=_integer(budget["pause_wait_seconds"], "budget.pause_wait_seconds"),
        budget_usd_reference=_number(budget["budget_usd_reference"], "budget.budget_usd_reference"),
        usd_limit_enforced=False,
        price_source=None,
    )
    seeds = _mapping(root["seeds"], "seeds", {"smoke", "validation", "final"})
    parsed_seeds = SeedConfig(**{name: _integers(seeds[name], f"seeds.{name}") for name in seeds})
    if not (set(parsed_seeds.smoke) <= set(parsed_seeds.validation) <= set(parsed_seeds.final)):
        raise ConfigError("Seed sets must be nested smoke <= validation <= final")
    concurrency = _mapping(root["concurrency"], "concurrency", {"max_active_tasks", "idle_user_counts"})
    parsed_concurrency = ConcurrencyConfig(
        max_active_tasks=_integer(concurrency["max_active_tasks"], "concurrency.max_active_tasks"),
        idle_user_counts=_integers(concurrency["idle_user_counts"], "concurrency.idle_user_counts"),
    )
    if (
        list(parsed_concurrency.idle_user_counts) != sorted(parsed_concurrency.idle_user_counts)
        or min(parsed_concurrency.idle_user_counts) < parsed_concurrency.max_active_tasks
    ):
        raise ConfigError("idle_user_counts must increase and accommodate max_active_tasks")
    resources = _mapping(root["resources"], "resources", {"memory_bytes", "cpus"})
    sampling = _mapping(root["sampling"], "sampling", {"interval_seconds"})
    policy = _mapping(root["execution_policy"], "execution_policy", {
        "ordinary_operations", "sensitive_operations", "human_approval",
    })
    if policy != {
        "ordinary_operations": "auto_within_scope",
        "sensitive_operations": "deny",
        "human_approval": False,
    } or policy["human_approval"] is not False:
        raise ConfigError("Execution policy requires scoped automatic execution, sensitive denial and no approval")
    return ExperimentConfig(
        schema_version=1,
        host_data_root=host_data_root,
        container_data_root=container_data_root,
        model=ModelConfig(**model),
        budget=parsed_budget,
        seeds=parsed_seeds,
        concurrency=parsed_concurrency,
        resources=ResourceConfig(
            memory_bytes=_integer(resources["memory_bytes"], "resources.memory_bytes"),
            cpus=_number(resources["cpus"], "resources.cpus"),
        ),
        sampling=SamplingConfig(interval_seconds=_number(sampling["interval_seconds"], "sampling.interval_seconds")),
        execution_policy=ExecutionPolicy(**policy),
    )
