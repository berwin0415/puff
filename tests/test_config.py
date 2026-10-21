"""Risk-focused checks for identity paths, state reuse and budget declarations."""

from dataclasses import asdict
from pathlib import Path

import pytest
import yaml

from runner.config import ConfigError, DEFAULT_CONFIG_PATH, load_config


def write_config(tmp_path, change=None):
    document = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    document["paths"]["data_root"] = str(tmp_path.resolve() / "data")
    if change is not None:
        change(document)
    path = tmp_path / "experiment.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    return path


def identity(**changes):
    return {
        "experiment_id": "tools-seed-101-repeat-1",
        "variant": "baseline",
        "candidate": "deepseek_harness",
        "user": "user-a",
        "session": "session-a",
        "run_id": "run-001",
        "date": "2026-10-20",
        **changes,
    }


def test_selected_budget_is_read_without_reading_credentials(monkeypatch):
    sentinel = "credential-values-must-never-be-read"
    for variable in ("MODEL_API_BASE_URL", "MODEL_API_KEY", "MODEL_NAME"):
        monkeypatch.setenv(variable, sentinel)
    config = load_config()
    assert str(config.data_root) == "/Users/hanbowen01/harness-lab-data"
    assert config.for_environment("container").data_root == Path("/data")
    assert (config.budget.active_timeout_seconds, config.budget.max_model_calls, config.budget.max_retries) == (300, 20, 2)
    assert config.budget.pause_wait_seconds == 600
    assert config.budget.budget_usd_reference == 1.0
    assert config.budget.usd_limit_enforced is False
    assert config.budget.price_source is None
    assert config.resources.memory_bytes == 6 * 1024 ** 3
    assert config.resources.cpus == 3
    assert config.concurrency.max_active_tasks == 2
    assert config.concurrency.idle_user_counts == (2, 5, 10)
    assert config.seeds.smoke == (101,)
    assert config.seeds.validation == (101, 202, 303)
    assert config.seeds.final == (101, 202, 303, 404, 505)
    assert sentinel not in repr(config)


def test_layout_preserves_archive_and_resume_chain_but_separates_variants(tmp_path):
    config_path = write_config(tmp_path)
    config = load_config(config_path)
    archive = config.data_root / "old"
    archive.mkdir(parents=True)
    sentinel = archive / "leave-alone"
    sentinel.write_text("archive content", encoding="utf-8")
    layout = config.initialize_layout()
    assert set(layout) == {"state", "workspaces", "fixtures", "budgets", "runs"}
    baseline = config.paths_for(**identity())
    baseline.state.mkdir(parents=True)
    baseline.budget.mkdir(parents=True)
    (baseline.state / "checkpoint").write_text("step 3", encoding="utf-8")
    (baseline.budget / "count").write_text("7", encoding="utf-8")

    # Loading in a replacement process uses the same persisted identity.
    resumed = load_config(config_path).paths_for(**identity())
    assert resumed == baseline
    assert (resumed.state / "checkpoint").read_text(encoding="utf-8") == "step 3"
    assert (resumed.budget / "count").read_text(encoding="utf-8") == "7"
    variant = config.paths_for(**identity(variant="non-idempotent-fault", run_id="run-002"))
    another_seed = config.paths_for(**identity(experiment_id="tools-seed-202-repeat-1", run_id="run-003"))
    for alternative in (variant, another_seed):
        for attribute in ("state", "workspace", "fixture", "budget", "run"):
            assert getattr(alternative, attribute) != getattr(baseline, attribute)
    assert sentinel.read_text(encoding="utf-8") == "archive content"
    assert list(archive.iterdir()) == [sentinel]


@pytest.mark.parametrize("field", ["experiment_id", "variant", "candidate", "user", "session", "run_id", "case"])
@pytest.mark.parametrize("component", ["../user-b", "/tmp/outside", "a/../b", r"a\b", "..", "", "old", "a\x00b"])
def test_untrusted_identity_cannot_select_other_paths(tmp_path, field, component):
    config = load_config(write_config(tmp_path))
    with pytest.raises(ConfigError):
        config.paths_for(**identity(**{field: component}))
    assert not config.data_root.exists()


@pytest.mark.parametrize("date", ["../old", "20261020", "2026-10-20/..", "2026-02-30"])
def test_evidence_date_cannot_escape_or_silently_change(tmp_path, date):
    config = load_config(write_config(tmp_path))
    with pytest.raises(ConfigError):
        config.paths_for(**identity(date=date))


@pytest.mark.parametrize("target", ["outside", "old", "missing"])
def test_symlink_cannot_escape_to_other_data_or_archive(tmp_path, target):
    config = load_config(write_config(tmp_path))
    config.initialize_layout()
    destination = config.data_root / "old" if target == "old" else tmp_path / target
    if target != "missing":
        destination.mkdir()
    (config.data_root / "state" / "tools-seed-101-repeat-1").symlink_to(destination, target_is_directory=True)
    with pytest.raises(ConfigError, match="symbolic"):
        config.paths_for(**identity())
    assert not (destination / "baseline").exists()


def test_existing_file_or_symlink_causes_layout_to_fail_before_creation(tmp_path):
    config = load_config(write_config(tmp_path))
    config.data_root.mkdir()
    (config.data_root / "runs").write_text("not a directory", encoding="utf-8")
    with pytest.raises(ConfigError):
        config.initialize_layout()
    assert not (config.data_root / "state").exists()


@pytest.mark.parametrize("change", [
    lambda doc: doc["budget"].update(max_model_calls=True),
    lambda doc: doc["budget"].update(max_model_calls=0),
    lambda doc: doc["budget"].update(max_retries=-1),
    lambda doc: doc["budget"].update(active_timeout_seconds="300"),
    lambda doc: doc["budget"].update(usd_limit_enforced=True),
    lambda doc: doc["budget"].update(price_source="assumed"),
    lambda doc: doc["resources"].update(cpus=float("inf")),
    lambda doc: doc["resources"].update(memory_bytes=-1),
    lambda doc: doc["model"].update(api_key_env="actual-secret"),
    lambda doc: doc["execution_policy"].update(human_approval=True),
    lambda doc: doc["execution_policy"].update(sensitive_operations="approve"),
    lambda doc: doc["budget"].update(max_model_call=20),
    lambda doc: doc["seeds"].update(validation=[101, 101]),
    lambda doc: doc["paths"].update(data_root="~/harness-lab-data"),
    lambda doc: doc["paths"].update(data_root="/data/../old"),
])
def test_invalid_or_misleading_limits_and_policies_are_rejected(tmp_path, change):
    with pytest.raises(ConfigError):
        load_config(write_config(tmp_path, change))


def test_duplicate_yaml_keys_are_not_silently_accepted(tmp_path):
    path = write_config(tmp_path)
    with path.open("a", encoding="utf-8") as stream:
        stream.write("\nschema_version: 1\n")
    with pytest.raises(ConfigError, match="Duplicate"):
        load_config(path)


def test_config_cannot_be_loaded_from_archive_or_symlink(tmp_path):
    path = write_config(tmp_path)
    archive = tmp_path / "old"
    archive.mkdir()
    archive_config = archive / "experiment.yaml"
    archive_config.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    with pytest.raises(ConfigError, match="archive"):
        load_config(archive_config)
    alias = tmp_path / "alias.yaml"
    alias.symlink_to(archive_config)
    with pytest.raises(ConfigError, match="symbolic"):
        load_config(alias)


def test_host_and_container_paths_have_identical_relative_layout(tmp_path):
    host = load_config(write_config(tmp_path))
    container = host.for_environment("container")
    host_paths = host.paths_for(**identity())
    container_paths = container.paths_for(**identity())
    for name in asdict(host_paths):
        assert getattr(host_paths, name).relative_to(host.data_root) == getattr(container_paths, name).relative_to(container.data_root)
