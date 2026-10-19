from __future__ import annotations

from pathlib import Path
import sys

from adapters.deepagents_langgraph import DeepAgentsLangGraphAdapter, collect_events


def test_collect_events_preserves_cursor_and_terminal_state() -> None:
    events = collect_events(
        "run-1",
        '{"event_type":"graph_started","payload":{"thread_id":"run-1"}}\n'
        '{"event_type":"graph_completed","payload":{"answer":"ok"}}\n',
        "",
        0,
    )
    assert [event["event_seq"] for event in events] == list(range(1, len(events) + 1))
    assert events[-2]["event_type"] == "graph_completed"
    assert events[-2]["state"] == "completed"
    assert events[-1]["event_type"] == "process_exited"


def test_preflight_accepts_external_archive_and_git_sources(tmp_path: Path) -> None:
    deep = tmp_path / "deepagents"
    lang = tmp_path / "langgraph"
    (deep / "libs/deepagents").mkdir(parents=True)
    (deep / "libs/deepagents/pyproject.toml").write_text('version = "0.7.28"\n', encoding="utf-8")
    (deep / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    (lang / "libs/langgraph").mkdir(parents=True)
    (lang / "libs/langgraph/pyproject.toml").write_text('version = "1.2.16"\n', encoding="utf-8")
    (lang / "LICENSE").write_text("MIT License\n", encoding="utf-8")
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=lang, check=True)
    subprocess.run(["git", "add", "."], cwd=lang, check=True)
    subprocess.run(["git", "-c", "user.email=test@example.com", "-c", "user.name=test", "commit", "-qm", "fixture"], cwd=lang, check=True)
    adapter = DeepAgentsLangGraphAdapter(
        source_path=deep,
        langgraph_source_path=lang,
        python_executable=Path(sys.executable),
        expected_langgraph_commit=None,
    )
    report = adapter.preflight()
    assert report.ok
    assert report.deepagents_source_kind == "github-archive"
    assert report.deepagents_version == "0.7.28"
    assert report.langgraph_version == "1.2.16"


def test_external_python_path_keeps_venv_symlink() -> None:
    adapter = DeepAgentsLangGraphAdapter(
        source_path="~/harness-lab-sources/deepagents",
        langgraph_source_path="~/harness-lab-sources/langgraph",
    )
    assert str(adapter.python_executable).endswith(".venv/bin/python")
