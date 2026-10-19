from __future__ import annotations

import json
from pathlib import Path

from scripts.run_deepseek_p0 import _local_event, _redactor, _write_events


def test_p0_artifact_redactor_scrubs_known_and_prefixed_credentials(tmp_path: Path) -> None:
    scrub = _redactor(["secret-value"])
    value = {
        "known": "secret-value",
        "header": "Authorization: Bearer secret-value",
        "prefixed": "sk-abcdefghijklmnop",
        "safe": "T1_OK sum=60",
    }

    redacted = scrub(value)

    assert "secret-value" not in json.dumps(redacted)
    assert "sk-abcdefghijklmnop" not in json.dumps(redacted)
    assert redacted["safe"] == "T1_OK sum=60"


def test_p0_event_writer_keeps_jsonl_sequence_and_shape(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    events = [_local_event("run-1", 1, "fixture/tool_observed", "completed", {"sum": 60})]

    _write_events(path, events, lambda value: value)

    stored = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    assert stored["run_id"] == "run-1"
    assert stored["event_seq"] == 1
    assert stored["event_type"] == "fixture/tool_observed"
    assert stored["payload"] == {"sum": 60}
