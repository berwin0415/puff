"""不能把 CLI 的普通429错误误当作已证明的本地预算拒绝。"""
import json

import pytest

from runner.model_probe import verify


@pytest.mark.parametrize("upstream_status,internal,expected", [(503, True, True), (429, True, False), (503, False, False)])
def test_codex_bodyless_limit_attribution(tmp_path, upstream_status, internal, expected):
    calls = [{"id": i, "outcome": "forwarded", "upstream_status": upstream_status} for i in (1, 2)]
    upstream = [{"id": i, "endpoint": "/v1/responses", "status": upstream_status} for i in (1, 2)]
    snapshot = {"budget": [{"cap": 2}], "calls": calls, "upstream": upstream,
                "candidate_status": "failed", "candidate_observed_limiter": False,
                "candidate_observed_rate_limit_status": True}
    manifest = {"candidate": "codex_sdk", "mock_only_internal_network": internal,
                "expected_candidate_image_id": "fixed-image", "expected_candidate_service": "codex-sdk",
                "rounds": [{"id": str(i), "image_id": "fixed-image", "compose_service": "codex-sdk",
                            "command_exit": 1} for i in (1, 2)],
                "first_candidate_deleted": True, "proxy_ids": ["old", "new"], "errors": []}
    (tmp_path / "budget-probe-manifest.json").write_text(json.dumps(manifest))
    for label in ("first", "second"):
        (tmp_path / label).mkdir()
        (tmp_path / label / "snapshot.json").write_text(json.dumps(snapshot))
    assert (verify(tmp_path) == 0) is expected
    result = json.loads((tmp_path / "budget-probe-verdict.json").read_text())
    assert result["checks"]["candidate_observed_limiter_both_rounds"] is expected
