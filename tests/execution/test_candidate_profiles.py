"""候选 profile 与现有 worker 常量一致性的离线验收。"""

from dataclasses import FrozenInstanceError
import json

import pytest

from execution import dsh_materials
from execution.candidate_profiles import (
    MODEL_BASE_URL,
    PROFILES,
    get_profile,
    main,
    profile_names,
)
from execution.worker_control import DA_ARGV, ENVIRONMENT as DA_ENVIRONMENT


def test_deepagents_profile_matches_worker_control():
    profile = get_profile("deepagents_langgraph")
    assert profile.supported is True
    assert profile.argv == tuple(DA_ARGV)
    assert profile.environment == DA_ENVIRONMENT
    assert profile.cwd == "/workspace"
    assert profile.config_dir.as_posix() == "/config"
    assert profile.state_dir.as_posix() == "/state"
    assert profile.workspace.as_posix() == "/workspace"
    assert profile.evidence_dir.as_posix() == "/evidence"
    assert profile.model_base_url == MODEL_BASE_URL
    assert profile.model_token_file == "/config/model-run.token"
    assert profile.worker_module == "execution.worker_control"
    assert profile.pause_semantics == "cooperative_drain_at_superstep_boundary"


def test_dsh_profile_matches_frozen_materials():
    profile = get_profile("deepseek_harness")
    assert profile.supported is True
    assert profile.argv == tuple(dsh_materials.ARGV)
    assert profile.cwd == dsh_materials.CWD
    assert profile.environment == dsh_materials.ENVIRONMENT
    assert profile.model_token_file == dsh_materials.TOKEN
    assert profile.pause_semantics == dsh_materials.SEMANTICS["pause"]
    assert profile.cancel_semantics == dsh_materials.SEMANTICS["cancel"]
    assert profile.worker_module == "execution.worker_control_dsh"


def test_openclaw_is_explicitly_unsupported():
    profile = get_profile("openclaw")
    assert profile.supported is False
    assert profile.argv == ()
    assert profile.cwd is None
    assert "not implemented" in profile.note


def test_unknown_profile_is_rejected():
    with pytest.raises(KeyError, match="unknown candidate profile"):
        get_profile("not-a-candidate")


def test_profiles_are_frozen_and_names_are_sorted():
    assert profile_names() == ("deepagents_langgraph", "deepseek_harness", "openclaw")
    with pytest.raises(FrozenInstanceError):
        PROFILES["deepagents_langgraph"].name = "changed"


def test_cli_lists_profiles_without_secrets(capsys):
    main()
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"deepagents_langgraph", "deepseek_harness", "openclaw"}
    assert payload["deepseek_harness"]["model_base_url"] == MODEL_BASE_URL
    assert "MODEL_API_KEY" not in json.dumps(payload)
