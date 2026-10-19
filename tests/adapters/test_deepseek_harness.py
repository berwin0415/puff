from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

from adapters.deepseek_harness import DeepSeekHarnessAdapter, SourcePreflightError, collect_events


def test_headless_command_keeps_prompt_and_credentials_out_of_flags(tmp_path: Path) -> None:
    adapter = DeepSeekHarnessAdapter(source_path=tmp_path, command=("node", "apps/cli/lib/bin.js"))

    command = adapter.build_headless_command("look up fixture value", extra_args=("--patch", "local.yml"))

    assert command == (
        "node",
        "apps/cli/lib/bin.js",
        "--profile",
        "headless",
        "--patch",
        "local.yml",
        "look up fixture value",
    )
    assert "DEEPSEEK_API_KEY" not in command
    with pytest.raises(ValueError):
        adapter.build_headless_command("  ")


def test_preflight_reports_expected_revision_and_clean_checkout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    adapter = DeepSeekHarnessAdapter(source_path=tmp_path, expected_commit="abc123")

    def fake_git(*args: str) -> str:
        if args == ("rev-parse", "HEAD"):
            return "abc123\n"
        assert args == ("status", "--porcelain")
        return ""

    monkeypatch.setattr(adapter, "_git", fake_git)
    report = adapter.preflight()

    assert report.ok is True
    assert report.actual_commit == "abc123"
    assert report.clean is True
    assert report.to_mapping()["path"] == str(tmp_path.resolve())


def test_preflight_can_be_inspected_without_raising_for_mismatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / ".git").mkdir()
    adapter = DeepSeekHarnessAdapter(source_path=tmp_path, expected_commit="expected")
    monkeypatch.setattr(adapter, "_git", lambda *args: "actual\n" if args == ("rev-parse", "HEAD") else "")

    report = adapter.preflight(raise_on_error=False)

    assert report.ok is False
    assert report.reason and "does not match" in report.reason
    with pytest.raises(SourcePreflightError):
        adapter.preflight()


def test_run_once_collects_output_events_without_real_model_api(tmp_path: Path) -> None:
    script = tmp_path / "fake_dsh.py"
    script.write_text(
        "import json, sys\n"
        "print('fixture answer')\n"
        "print('dsh: reasoning:', file=sys.stderr)\n"
        "print(json.dumps({'event_type': 'tool_call', 'payload': {'name': 'fixture.lookup'}}), file=sys.stderr)\n",
        encoding="utf-8",
    )
    adapter = DeepSeekHarnessAdapter(source_path=tmp_path, command=(sys.executable, str(script)))

    result = adapter.run_once({"prompt": "read fixture"}, env={"DEEPSEEK_API_KEY": "test-only"})

    assert result.ok is True
    assert result.stdout == "fixture answer\n"
    assert result.command[-1] == "read fixture"
    assert all("test-only" not in json.dumps(event) for event in result.events)
    assert [event["event_seq"] for event in result.events] == list(range(1, len(result.events) + 1))
    assert result.events[0]["event_type"] == "process_started"
    assert any(event["event_type"] == "assistant_output" for event in result.events)
    assert any(event["event_type"] == "candidate/tool_call" for event in result.events)
    assert result.events[-1]["event_type"] == "process_exited"


def test_submit_and_stream_events_are_cursor_reconnectable(tmp_path: Path) -> None:
    adapter = DeepSeekHarnessAdapter(source_path=tmp_path)
    run_id = adapter.submit({"prompt": "queued"})

    cursor, events = adapter.stream_events(run_id)
    assert cursor == 1
    assert list(events)[0]["event_type"] == "run_queued"
    next_cursor, next_events = adapter.stream_events(run_id, after_seq=cursor)
    assert next_cursor == cursor
    assert list(next_events) == []


def test_collect_events_marks_failed_process() -> None:
    events = collect_events("run-1", "", "dsh: failed", 2)

    assert events[-1]["state"] == "aborted"
    assert events[-1]["error"]["category"] == "process_exit"
