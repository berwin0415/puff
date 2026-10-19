"""OpenClaw adapter preflight, event mapping and live smoke tests."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from adapters.openclaw import OpenClawAdapter, collect_events


def _fake_source(tmp_path: Path) -> Path:
    source = tmp_path / "openclaw"
    source.mkdir()
    (source / "openclaw.mjs").write_text("// fixture\n", encoding="utf-8")
    (source / "package.json").write_text('{"name":"openclaw","version":"2026.10.5"}\n', encoding="utf-8")
    (source / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=source, check=True)
    subprocess.run(["git", "add", "."], cwd=source, check=True)
    subprocess.run(
        ["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "fixture"],
        cwd=source,
        check=True,
    )
    return source


def _fake_cli(tmp_path: Path, envelope: dict) -> Path:
    script = tmp_path / "fake_openclaw.py"
    script.write_text(
        "import json\nprint(json.dumps(" + repr(envelope) + "))\n",
        encoding="utf-8",
    )
    return script


def test_preflight_accepts_a_pinned_checkout(tmp_path: Path) -> None:
    source = _fake_source(tmp_path)
    adapter = OpenClawAdapter(source_path=source, expected_commit=None)
    report = adapter.preflight()
    assert report.ok
    assert report.version == "2026.10.5"
    assert report.license_present
    assert report.commit


def test_event_mapping_marks_success_and_failure() -> None:
    success = collect_events(
        "run-1",
        {"ok": True, "status": "ok", "final": "OPENCLAW_API_OK", "model": "m", "provider": "lab"},
        0,
    )
    assert [event["event_type"] for event in success] == ["process_started", "assistant_output", "process_exited"]
    assert success[1]["state"] == "completed"
    assert success[-1]["state"] == "completed"

    failure = collect_events(
        "run-2",
        {"ok": False, "status": "error", "error": {"kind": "model", "message": "boom"}},
        1,
    )
    assert failure[1]["event_type"] == "candidate_error"
    assert failure[1]["state"] == "aborted"
    assert failure[-1]["state"] == "aborted"
    assert failure[-1]["error"]["category"] == "process_exit"


def test_run_once_maps_envelope_and_writes_evidence(tmp_path: Path) -> None:
    source = _fake_source(tmp_path)
    envelope = {
        "ok": True,
        "status": "ok",
        "final": "fixture-alpha",
        "payloads": [{"text": "fixture-alpha"}],
        "usage": {"input": 10, "output": 2, "total": 12},
        "model": "deepseek-v4-flash",
        "provider": "lab",
        "sessionId": "session-1",
    }
    cli = _fake_cli(tmp_path, envelope)
    adapter = OpenClawAdapter(
        source_path=source,
        cli_command=[sys.executable, str(cli)],
        expected_commit=None,
        artifact_root=tmp_path / "artifacts",
    )
    result = adapter.run_once(
        "Return fixture-alpha.",
        run_id="openclaw-run-1",
        env={
            "MODEL_NAME": "deepseek-v4-flash",
            "MODEL_API_BASE_URL": "http://example.invalid/v1",
            "MODEL_API_KEY": "test-key",
        },
    )
    assert result.ok
    assert adapter.result("openclaw-run-1")["final"] == "fixture-alpha"
    run_dir = tmp_path / "artifacts" / "openclaw-run-1"
    assert (run_dir / "manifest.json").exists()
    assert (run_dir / "events.jsonl").exists()
    assert (run_dir / "envelope.json").exists()
    assert (run_dir / "openclaw.json5").exists()
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["state"] == "completed"
    assert "test-key" not in (run_dir / "manifest.json").read_text(encoding="utf-8")


LIVE = os.environ.get("RUN_LIVE_MODEL_TESTS") == "1" and Path("../openclaw").exists()


@pytest.mark.skipif(not LIVE, reason="set RUN_LIVE_MODEL_TESTS=1 and provide ../openclaw")
def test_live_agent_exec_smoke(tmp_path: Path) -> None:
    env = {}
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            env[key] = value.strip().strip('"').strip("'")
    adapter = OpenClawAdapter(artifact_root=tmp_path)
    result = adapter.run_once(
        "Reply with exactly OPENCLAW_API_OK and no other text.",
        timeout=180,
        env=env,
    )
    assert result.ok, result.stderr[-1000:]
    assert adapter.result(result.run_id)["final"] == "OPENCLAW_API_OK"
