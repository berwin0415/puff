#!/usr/bin/env python3
"""Run the first DeepSeek Harness P0 tasks against the local fixtures.

The script intentionally keeps the candidate checkout outside this repository.
It loads the local ``.env`` without printing it, maps the lab's generic model
variables to the names understood by DeepSeek Harness, and writes only
redacted evidence below ``HARNESS_LAB_ARTIFACT_ROOT``.  T1 is a real headless
tool run.  T2 runs the common Playwright baseline and separately records the
headless profile's reproducible browser capability block.

Examples::

    .venv/bin/python scripts/run_deepseek_p0.py --task all
    .venv/bin/python scripts/run_deepseek_p0.py --task t1
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
from typing import Any, Mapping
import uuid


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from adapters.deepseek_harness import DeepSeekHarnessAdapter  # noqa: E402
from execution.browser.smoke import run_smoke  # noqa: E402
from fixtures.service.server import create_server  # noqa: E402


SOURCE_PATH = ROOT.parent / "deepseek-harness"
SOURCE_COMMIT = "dd6322d604e00eec1ba5e0c8541159906a21094a"
ARTIFACT_CANDIDATE = "deepseek_harness"
DEFAULT_TIMEOUT = 120.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_dotenv(path: Path) -> dict[str, str]:
    """Load simple KEY=VALUE entries without requiring python-dotenv."""

    values: dict[str, str] = {}
    if not path.is_file():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def _runtime_env() -> tuple[dict[str, str], str]:
    """Return a child environment and the configured model name.

    Existing shell variables win over the local file.  The API key is copied
    into the child environment only; it is never included in a manifest or
    command argument.
    """

    env = dict(os.environ)
    for key, value in _load_dotenv(ROOT / ".env").items():
        env.setdefault(key, value)
    api_key = env.get("MODEL_API_KEY") or env.get("DEEPSEEK_API_KEY")
    base_url = env.get("MODEL_API_BASE_URL") or env.get("DEEPSEEK_BASE_URL")
    if not api_key:
        raise RuntimeError("MODEL_API_KEY or DEEPSEEK_API_KEY is required")
    if not base_url:
        raise RuntimeError("MODEL_API_BASE_URL or DEEPSEEK_BASE_URL is required")
    env["DEEPSEEK_API_KEY"] = api_key
    env["DEEPSEEK_BASE_URL"] = base_url
    model = env.get("MODEL_NAME", "deepseek-v4-flash")
    return env, model


def _redactor(secret_values: list[str]) -> Any:
    values = [value for value in secret_values if value]
    # Keep this intentionally broad enough to catch credentials echoed by a
    # provider while avoiding any assumption about a particular key prefix.
    key_pattern = re.compile(
        r"(?i)(?P<prefix>api[_-]?key|authorization|bearer|secret|token)"
        r"(?P<sep>\s*[:=]\s+|\s+)(?P<value>[^\s,\"'}]+)"
    )
    sk_pattern = re.compile(r"\bsk-[A-Za-z0-9._=-]{8,}\b")

    def scrub_string(value: str) -> str:
        for secret in values:
            value = value.replace(secret, "[REDACTED]")
        value = key_pattern.sub(lambda match: f"{match.group('prefix')}{match.group('sep')}[REDACTED]", value)
        return sk_pattern.sub("[REDACTED]", value)

    def scrub(value: Any) -> Any:
        if isinstance(value, str):
            return scrub_string(value)
        if isinstance(value, Mapping):
            return {str(key): scrub(item) for key, item in value.items()}
        if isinstance(value, list):
            return [scrub(item) for item in value]
        if isinstance(value, tuple):
            return [scrub(item) for item in value]
        return value

    return scrub


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_events(path: Path, events: list[Mapping[str, Any]], scrub: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for event in events:
            stream.write(json.dumps(scrub(event), ensure_ascii=False, sort_keys=True) + "\n")


def _local_event(
    run_id: str,
    event_seq: int,
    event_type: str,
    state: str,
    payload: Mapping[str, Any],
    *,
    error: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "event_id": str(uuid.uuid4()),
        "run_id": run_id,
        "event_seq": event_seq,
        "occurred_at": _utc_now(),
        "candidate": ARTIFACT_CANDIDATE,
        "candidate_version": SOURCE_COMMIT,
        "event_type": event_type,
        "previous_state": None,
        "state": state,
        "parent_event_id": None,
        "candidate_event_id": None,
        "tool_call": None,
        "model_call": None,
        "error": dict(error) if error else None,
        "payload": dict(payload),
    }


class _Fixture:
    def __init__(self, state_path: Path) -> None:
        self.state_path = state_path
        self.server = create_server("127.0.0.1", 0, state_path)
        self.thread = threading.Thread(target=self.server.serve_forever, name="fixture", daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def __enter__(self) -> "_Fixture":
        self.thread.start()
        return self

    def snapshot(self) -> dict[str, Any]:
        if not self.state_path.is_file():
            return {"effects": {}, "requests": [], "failure_attempts": {}}
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"effects": {}, "requests": [], "failure_attempts": {}}
        return value if isinstance(value, dict) else {}

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _artifact_dir(root: Path, task_id: str, run_id: str) -> Path:
    date = datetime.now(timezone.utc).date().isoformat()
    path = root / date / ARTIFACT_CANDIDATE / task_id / run_id
    (path / "state").mkdir(parents=True, exist_ok=True)
    (path / "artifacts").mkdir(parents=True, exist_ok=True)
    return path


def _adapter() -> DeepSeekHarnessAdapter:
    return DeepSeekHarnessAdapter(
        source_path=SOURCE_PATH,
        expected_commit=SOURCE_COMMIT,
        # Launch the already-built external CLI directly so the candidate's
        # process cwd can be an artifact-local workspace.  Running ``pnpm dsh``
        # from the checkout would make that checkout the agent workspace and
        # allow a model task to leave untracked files there.
        command=("node", str(SOURCE_PATH / "apps/cli/lib/bin.js")),
        profile="headless",
        execute_on_submit=False,
    )


def _base_manifest(
    *,
    run_id: str,
    task_id: str,
    started_at: str,
    source: Mapping[str, Any],
    model: str,
    fixture_url: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "run_id": run_id,
        "candidate": ARTIFACT_CANDIDATE,
        "candidate_version": SOURCE_COMMIT,
        "task_id": task_id,
        "state": "running",
        "started_at": started_at,
        "source_preflight": dict(source),
        "runtime": {
            "profile": "headless",
            "command": ["node", "apps/cli/lib/bin.js", "--profile", "headless"],
            "model": model,
            "credential_source": "environment:DEEPSEEK_API_KEY",
            "base_url_source": "environment:DEEPSEEK_BASE_URL",
        },
        "fixture": {"base_url": fixture_url, "scope": "ephemeral-local"},
        "evidence": {
            "events": "events.jsonl",
            "result": "result.json",
            "state": "state/",
            "artifacts": "artifacts/",
        },
    }


def _run_t1(root: Path, env: Mapping[str, str], model: str, source: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    started = _utc_now()
    path = _artifact_dir(root, "t1-tools", run_id)
    state_path = path / "state" / "fixture-state.json"
    workspace = path / "artifacts" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    task_env = dict(env)
    task_env["DSH_HOME"] = str(path / "state" / "dsh-home")
    adapter = _adapter()
    with _Fixture(state_path) as fixture:
        prompt = (
            f"Use the bash tool to execute the following deterministic tool task against {fixture.base_url}. "
            f"First make one HTTP GET request with curl to {fixture.base_url}/api/items?limit=3 and parse its JSON. "
            "Calculate the sum of the three item values; it must be 60. Then make one HTTP POST request to "
            f"{fixture.base_url}/api/side-effects with JSON {{\"operation\":\"t1\",\"payload\":{{\"sum\":60}}}} "
            "and the header Idempotency-Key: deepseek-t1-smoke. Execute the commands; do not only describe them. "
            "Reply with exactly one line beginning T1_OK and include the computed sum and POST status."
        )
        result = adapter.run_once({"prompt": prompt}, run_id=run_id, env=task_env, timeout=timeout, cwd=workspace)
        time.sleep(0.05)
        snapshot = fixture.snapshot()

    effects = snapshot.get("effects") if isinstance(snapshot.get("effects"), dict) else {}
    requests = snapshot.get("requests") if isinstance(snapshot.get("requests"), list) else []
    record = effects.get("deepseek-t1-smoke") if isinstance(effects, dict) else None
    validation = {
        "model_run_ok": result.ok,
        "answer_marker": bool(re.search(r"\bT1_OK\b", result.stdout)),
        "sum_60": bool(record and record.get("record", {}).get("payload", {}).get("sum") == 60),
        "idempotent_effect_count": len(effects),
        "fixture_get_count": sum(1 for request in requests if request.get("method") == "GET" and request.get("path") == "/api/items"),
        "fixture_post_count": sum(1 for request in requests if request.get("method") == "POST" and request.get("path") == "/api/side-effects"),
    }
    validation["tool_verified"] = bool(
        validation["model_run_ok"]
        and validation["answer_marker"]
        and validation["sum_60"]
        and validation["idempotent_effect_count"] == 1
        and validation["fixture_post_count"] >= 1
    )
    events = [dict(event) for event in result.events]
    next_seq = max((int(event["event_seq"]) for event in events), default=0) + 1
    events.append(
        _local_event(
            run_id,
            next_seq,
            "fixture/tool_observed",
            "completed" if validation["tool_verified"] else "aborted",
            {"requests": requests, "effects": effects, "validation": validation},
        )
    )
    scrub = _redactor([env.get("MODEL_API_KEY", ""), env.get("DEEPSEEK_API_KEY", "")])
    result_mapping = {
        "run_id": run_id,
        "task_id": "t1-tools",
        "state": "completed" if validation["tool_verified"] else ("aborted" if not result.timed_out else "aborted"),
        "candidate_result": result.to_mapping(),
        "validation": validation,
        "fixture_state": snapshot,
    }
    manifest = _base_manifest(run_id=run_id, task_id="t1-tools", started_at=started, source=source, model=model, fixture_url=fixture.base_url)
    manifest.update({"state": result_mapping["state"], "ended_at": _utc_now(), "validation": validation})
    _write_events(path / "events.jsonl", events, scrub)
    _write_json(path / "result.json", scrub(result_mapping))
    _write_json(path / "manifest.json", scrub(manifest))
    return {"task_id": "t1-tools", "run_id": run_id, "state": manifest["state"], "tool_verified": validation["tool_verified"], "path": str(path)}


def _run_t2(root: Path, env: Mapping[str, str], model: str, source: Mapping[str, Any], timeout: float) -> dict[str, Any]:
    run_id = str(uuid.uuid4())
    started = _utc_now()
    path = _artifact_dir(root, "t2-browser", run_id)
    candidate_state_path = path / "state" / "candidate-fixture-state.json"
    workspace = path / "artifacts" / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    task_env = dict(env)
    task_env["DSH_HOME"] = str(path / "state" / "dsh-home")
    adapter = _adapter()
    with _Fixture(candidate_state_path) as candidate_fixture:
        prompt = (
            f"Use a browser automation tool to open {candidate_fixture.base_url}/browser/, fill #name with "
            "harness-lab, submit the form, and verify the result text is accepted:harness-lab. Perform the "
            "interaction rather than describing it. If this headless runtime has no browser capability, return "
            "one line beginning T2_BLOCKED with the missing capability; otherwise return one line beginning T2_OK "
            "with the observed result."
        )
        candidate_result = adapter.run_once({"prompt": prompt}, run_id=run_id, env=task_env, timeout=timeout, cwd=workspace)
        time.sleep(0.05)
        candidate_snapshot = candidate_fixture.snapshot()

    baseline_state_path = path / "state" / "browser-baseline-state.json"
    with _Fixture(baseline_state_path) as baseline_fixture:
        browser_dir = path / "artifacts" / "browser"
        try:
            baseline = run_smoke(baseline_fixture.base_url, browser_dir)
            baseline_error = None
        except Exception as exc:  # pragma: no cover - environment diagnostics are persisted
            baseline = None
            baseline_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.05)
        baseline_snapshot = baseline_fixture.snapshot()

    browser_submit_count = sum(
        1
        for request in baseline_snapshot.get("requests", [])
        if request.get("method") == "POST" and request.get("path") == "/browser/submit"
    )
    candidate_submit_count = sum(
        1
        for request in candidate_snapshot.get("requests", [])
        if request.get("method") == "POST" and request.get("path") == "/browser/submit"
    )
    baseline_verified = bool(baseline and baseline.get("result") == "accepted:harness-lab" and browser_submit_count == 1)
    # The shipped headless profile has no browser Connection by construction.
    # A model may still use its shell tool to issue HTTP requests (including a
    # direct POST to the fixture), but that is not browser automation and must
    # not be counted as a successful T2.  Keep the attempted request count as
    # evidence while classifying the candidate capability from the profile
    # contract.
    candidate_blocked = True
    validation = {
        "execution_playwright_verified": baseline_verified,
        "candidate_browser_submit_count": candidate_submit_count,
        "candidate_headless_no_browser": candidate_blocked,
        "candidate_direct_http_attempts": len(candidate_snapshot.get("requests", [])),
        "candidate_model_run_ok": candidate_result.ok,
        "candidate_answer": candidate_result.stdout.strip(),
        "baseline_error": baseline_error,
        "baseline_result": baseline,
        "blocked_reason": "DeepSeek Harness headless profile mounts no browser Connection, HTTP server, Web runtime, or browser client",
        "source_evidence": "../deepseek-harness/apps/cli/reference/README.md#app-arguments",
    }
    events = [dict(event) for event in candidate_result.events]
    next_seq = max((int(event["event_seq"]) for event in events), default=0) + 1
    events.append(
        _local_event(
            run_id,
            next_seq,
            "execution/browser_smoke",
            "completed" if baseline_verified else "aborted",
            {"track": "execution_playwright", "result": baseline, "fixture_requests": baseline_snapshot.get("requests", [])},
            error={"category": "browser_baseline", "message": baseline_error} if baseline_error else None,
        )
    )
    next_seq += 1
    events.append(
        _local_event(
            run_id,
            next_seq,
            "candidate/browser_capability_blocked",
            "blocked" if candidate_blocked else ("completed" if candidate_result.ok else "aborted"),
            {"track": "deepseek_headless", "candidate_fixture_requests": candidate_snapshot.get("requests", []), "reason": validation["blocked_reason"]},
            error={"category": "unsupported_capability", "message": validation["blocked_reason"]} if candidate_blocked else None,
        )
    )
    scrub = _redactor([env.get("MODEL_API_KEY", ""), env.get("DEEPSEEK_API_KEY", "")])
    overall_state = "blocked" if candidate_blocked else ("completed" if baseline_verified and candidate_result.ok else "aborted")
    result_mapping = {
        "run_id": run_id,
        "task_id": "t2-browser",
        "state": overall_state,
        "candidate_result": candidate_result.to_mapping(),
        "validation": validation,
        "candidate_fixture_state": candidate_snapshot,
        "baseline_fixture_state": baseline_snapshot,
    }
    manifest = _base_manifest(run_id=run_id, task_id="t2-browser", started_at=started, source=source, model=model, fixture_url=candidate_fixture.base_url)
    manifest.update({"state": overall_state, "ended_at": _utc_now(), "validation": validation, "capability_status": {"browser": "blocked" if candidate_blocked else "observed"}})
    _write_events(path / "events.jsonl", events, scrub)
    _write_json(path / "result.json", scrub(result_mapping))
    _write_json(path / "manifest.json", scrub(manifest))
    return {"task_id": "t2-browser", "run_id": run_id, "state": overall_state, "browser_baseline_verified": baseline_verified, "candidate_blocked": candidate_blocked, "path": str(path)}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=("t1", "t2", "all"), default="all")
    parser.add_argument("--artifact-root", default=None, help="external artifact root (defaults to HARNESS_LAB_ARTIFACT_ROOT)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="per candidate task timeout in seconds")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    env, model = _runtime_env()
    root = Path(args.artifact_root or env.get("HARNESS_LAB_ARTIFACT_ROOT", "~/harness-lab-data/runs")).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    adapter = _adapter()
    source = adapter.preflight(raise_on_error=False).to_mapping()
    if not source["ok"]:
        raise SystemExit(f"DeepSeek source preflight failed: {source.get('reason')}")
    results: list[dict[str, Any]] = []
    if args.task in {"t1", "all"}:
        results.append(_run_t1(root, env, model, source, args.timeout))
    if args.task in {"t2", "all"}:
        results.append(_run_t2(root, env, model, source, args.timeout))
    print(json.dumps({"candidate": ARTIFACT_CANDIDATE, "artifact_root": str(root), "results": results}, ensure_ascii=False, indent=2))
    # A browser capability block is an expected result for the shipped
    # headless profile.  T1 failures remain a non-zero process status.
    return 0 if all(item.get("state") in {"completed", "blocked"} and (item.get("tool_verified", True)) for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
