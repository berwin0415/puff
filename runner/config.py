"""严格校验实验配置和路径；此模块从不读取凭证。

宿主机和容器采用相同相对布局。在挂载于 /data 的容器中调用
``config.for_environment("container")``。只有恢复链可复用实验、变体、用户和会话 ID；
独立种子或重复实验必须使用不同的实验或变体 ID。"""

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
    """配置无效，或实验路径不安全。"""


class _UniqueKeyLoader(yaml.SafeLoader):
    """拒绝重复 YAML 键，不静默保留最后一个值。"""


def _unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=True)
        if not isinstance(key, str):
            raise ConfigError("配置映射的键必须是字符串")
        if key in mapping:
            raise ConfigError(f"重复的配置键：{key}")
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
            raise ConfigError("environment 必须为 host 或 container")
        return replace(self, environment=environment)

    def initialize_layout(self) -> dict[str, Path]:
        """仅创建当前实验的五个目录，不包含归档。"""
        root = _root(self.data_root, "data_root")
        directories = {name: root / name for name in _LAYOUT}
        # 创建前先检查全部路径：已有不安全子路径时，
        # 不能留下部分初始化的目录布局。
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
        """计算路径，不创建目录，也不读取归档数据。

        复用 ID 可在容器重启后获得同一状态和预算。
        不同变体分别隔离状态、工作区、Fixture 账本和预算。
        独立实验的证据运行 ID 必须唯一。"""
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
        raise ConfigError(f"无效路径分量：{name}")
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
    raise ConfigError("date 必须是日期对象或 YYYY-MM-DD 格式字符串")


def _no_symlinks(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ConfigError("实验路径不能包含符号链接")
        if current.exists() and not current.is_dir():
            raise ConfigError("实验目录路径已被文件占用")


def _root(value: str | Path, name: str) -> Path:
    if not isinstance(value, (str, Path)):
        raise ConfigError(f"{name} 必须是绝对路径")
    path = Path(value)
    if (
        not path.is_absolute() or path == Path(path.anchor)
        or ".." in path.parts or any(part.lower() == "old" for part in path.parts)
        or "~" in str(path) or "\\" in str(path)
        or any(ord(character) < 32 for character in str(path))
    ):
        raise ConfigError(f"{name} 必须是绝对目录路径，且不含归档目录或路径穿越")
    _no_symlinks(path)
    if path.exists() and not path.is_dir():
        raise ConfigError(f"{name} 必须是目录")
    return path


def _safe_path(root: Path, path: Path) -> None:
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ConfigError("实验路径超出数据根目录") from exc
    if not relative.parts or relative.parts[0] not in _LAYOUT:
        raise ConfigError("实验路径不在当前目录布局内")
    _no_symlinks(path)
    if path.exists() and not path.is_dir():
        raise ConfigError("实验目录路径已被文件占用")


def _mapping(value: Any, name: str, expected: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{name} 必须是映射")
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing or extra:
        raise ConfigError(f"{name}：缺少键 {sorted(missing)}；未知键 {sorted(extra)}")
    return value


def _integer(value: Any, name: str, *, minimum: int = 1) -> int:
    if type(value) is not int or value < minimum:
        raise ConfigError(f"{name} 必须是整数，且不小于 {minimum}")
    return value


def _number(value: Any, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise ConfigError(f"{name} 必须是有限正数")
    return float(value)


def _integers(value: Any, name: str) -> tuple[int, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(f"{name} 必须是非空整数列表")
    values = tuple(_integer(item, name, minimum=0) for item in value)
    if len(set(values)) != len(values):
        raise ConfigError(f"{name} 包含重复值")
    return values


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> ExperimentConfig:
    """严格读取 YAML；模型字段只包含环境变量名，不包含秘密值。"""
    source = Path(path).absolute()
    if ".." in source.parts or any(part.lower() == "old" for part in source.parts):
        raise ConfigError("不能从归档目录或路径穿越位置加载配置")
    _no_symlinks(source.parent)
    if source.is_symlink():
        raise ConfigError("配置文件不能是符号链接")
    try:
        with source.open(encoding="utf-8") as stream:
            document = yaml.load(stream, Loader=_UniqueKeyLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigError("无法读取实验配置") from exc
    root = _mapping(document, "configuration", {
        "schema_version", "paths", "model", "budget", "seeds", "concurrency",
        "resources", "sampling", "execution_policy",
    })
    if type(root["schema_version"]) is not int or root["schema_version"] != 1:
        raise ConfigError("不支持的配置 schema_version")
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
        raise ConfigError("模型配置必须引用三个已批准的环境变量名")
    budget = _mapping(root["budget"], "budget", {
        "active_timeout_seconds", "max_model_calls", "max_retries", "pause_wait_seconds",
        "budget_usd_reference", "usd_limit_enforced", "price_source",
    })
    if budget["usd_limit_enforced"] is not False or budget["price_source"] is not None:
        raise ConfigError("当前无法强制执行金额上限；usd_limit_enforced 须为 false，price_source 须为 null")
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
        raise ConfigError("种子集合必须满足包含关系 smoke <= validation <= final")
    concurrency = _mapping(root["concurrency"], "concurrency", {"max_active_tasks", "idle_user_counts"})
    parsed_concurrency = ConcurrencyConfig(
        max_active_tasks=_integer(concurrency["max_active_tasks"], "concurrency.max_active_tasks"),
        idle_user_counts=_integers(concurrency["idle_user_counts"], "concurrency.idle_user_counts"),
    )
    if (
        list(parsed_concurrency.idle_user_counts) != sorted(parsed_concurrency.idle_user_counts)
        or min(parsed_concurrency.idle_user_counts) < parsed_concurrency.max_active_tasks
    ):
        raise ConfigError("idle_user_counts 必须递增，且能容纳 max_active_tasks")
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
        raise ConfigError("执行策略必须为作用域内自动执行、拒绝敏感操作且不等待审批")
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
