"""仅在容器内运行的模拟模型上游，以及独立候选框架预算检查。

桩服务始终返回 503，只记录序号、时间和固定端点标签；
不保留请求正文、请求头或凭证。"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import sqlite3


ENDPOINTS = {"/v1/chat/completions", "/v1/responses"}


class ProbeLedger:
    def __init__(self, database: Path):
        self.database = Path(database)
        self.database.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("CREATE TABLE IF NOT EXISTS requests (id INTEGER PRIMARY KEY AUTOINCREMENT, received_at TEXT NOT NULL, endpoint TEXT NOT NULL, status INTEGER NOT NULL CHECK(status=503))")

    def connect(self):
        return sqlite3.connect(self.database, timeout=30)

    def record(self, endpoint: str):
        # 不持久化任意 URL，它可能包含凭证。
        label = endpoint if endpoint in ENDPOINTS else "unsupported"
        with self.connect() as db:
            db.execute("INSERT INTO requests(received_at,endpoint,status) VALUES(?,?,503)",
                       (datetime.now(timezone.utc).isoformat(), label))


def create_server(database: Path, host="0.0.0.0", port=8090):
    ledger = ProbeLedger(database)

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_args):
            pass

        def reply(self, status, value):
            data = json.dumps(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404,
                       {"status": "ok"} if self.path == "/health" else {"error": "not_found"})

        def do_POST(self):
            ledger.record(self.path)
            # 丢弃有大小限制的请求字节，让真实代理完成上传。
            # 不解析、记录或保留正文的任何部分。
            try:
                length = int(self.headers.get("Content-Length", "-1"))
            except ValueError:
                length = -1
            if 0 <= length <= 16 * 1024 * 1024:
                self.connection.settimeout(10)
                try:
                    while length:
                        chunk = self.rfile.read(min(length, 65536))
                        if not chunk:
                            break
                        length -= len(chunk)
                except (OSError, TimeoutError):
                    pass
            self.reply(503, {"error": {"type": "probe_unavailable", "code": "probe_unavailable",
                                       "message": "合成上游不可用"}})

    server = ThreadingHTTPServer((host, port), Handler)
    server.daemon_threads = True
    return server


def save(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def read_rows(database: Path, query: str):
    with sqlite3.connect(database.as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        return [dict(row) for row in db.execute(query)]


def prepare(run_root: Path, state_root: Path):
    for name in ("first", "second"):
        (run_root / name / "candidate").mkdir(parents=True, exist_ok=False)
        (state_root / name).mkdir(parents=True, exist_ok=False)
    (run_root / "model-stub").mkdir(exist_ok=False)
    value = json.loads((run_root / "input.json").read_text())
    value.update({"model_call_cap": 2, "model_name": "probe-model", "all_model_traffic_mocked": True,
                  "purpose": "候选框架真实内部重试及持久化预算约束"})
    save(run_root / "input.json", value)


def snapshot(budget: Path, upstream: Path, candidate_dir: Path):
    result = json.loads((candidate_dir / "result.json").read_text())
    raw_file = candidate_dir / "raw-events.jsonl"
    raw = raw_file.read_text() if raw_file.exists() else ""
    text = json.dumps(result) + raw
    # 固定 DSH llm-retry/src/index.ts 追加类型 llm/retry；SDK
    # server.ts 以 session.event 方法和 params.event 转发。
    notifications = [json.loads(line) for line in raw.splitlines() if line.strip()]
    retry_count = sum(notification.get("method") == "session.event"
                      and notification.get("params", {}).get("event", {}).get("type") == "llm/retry"
                      for notification in notifications)
    # 这些标记由限流器发出，不来自合成上游。
    limiter_seen = any(marker in text for marker in ("call_limit_exceeded", "模型请求次数已达上限"))
    return {
        "budget": read_rows(budget, "SELECT cap FROM budget WHERE id=1"),
        "calls": read_rows(budget, "SELECT id,started_at,path,outcome,upstream_status FROM calls ORDER BY id"),
        "upstream": read_rows(upstream, "SELECT id,received_at,endpoint,status FROM requests ORDER BY id"),
        "candidate_status": result.get("status"), "candidate_stop_reason": result.get("stop_reason"),
        "candidate_observed_limiter": limiter_seen,
        # 固定 Codex CLI 会丢弃非成功响应正文。单独保留这项
        # 较弱的原生观测；验证还必须证明
        # 隔离的合成上游只可能返回 503。
        "candidate_observed_rate_limit_status": any(
            row.get("type") == "error" and row.get("message") == "exceeded retry limit, last status: 429 Too Many Requests"
            for row in notifications),
        "candidate_native_retry_recorded": retry_count > 0,
        "candidate_native_retry_count": retry_count,
    }


def verify(run_root: Path):
    manifest = json.loads((run_root / "budget-probe-manifest.json").read_text())
    first = json.loads((run_root / "first/snapshot.json").read_text())
    second = json.loads((run_root / "second/snapshot.json").read_text())
    rounds = manifest.get("rounds", [])
    checks = {
        "mock_only_internal_network": manifest.get("mock_only_internal_network") is True,
        "same_real_candidate_image": len(rounds) == 2 and bool(rounds[0].get("image_id")) and rounds[0]["image_id"] == rounds[1].get("image_id"),
        "expected_candidate_service_and_image": bool(manifest.get("expected_candidate_image_id"))
            and len(rounds) == 2 and all(row.get("image_id") == manifest["expected_candidate_image_id"]
                and row.get("compose_service") == manifest.get("expected_candidate_service") for row in rounds),
        "different_candidate_containers": len(rounds) == 2 and bool(rounds[0].get("id")) and rounds[0]["id"] != rounds[1].get("id"),
        "first_candidate_deleted_before_second": manifest.get("first_candidate_deleted") is True,
        "different_proxy_containers": len(manifest.get("proxy_ids", [])) == 2 and len(set(manifest["proxy_ids"])) == 2,
        "candidate_failures_are_bounded": len(rounds) == 2 and all(row.get("command_exit") not in (None, 0, 124) for row in rounds),
        "candidate_really_retried": (first.get("candidate_native_retry_recorded") is True
            if manifest.get("candidate") == 'deepseek_harness'
            else len(first.get("calls", [])) == 2 and all(
                row.get("outcome") == "forwarded" and row.get("upstream_status") == 503
                for row in first.get("calls", []))),
        "candidate_observed_limiter_both_rounds": all(
            row.get("candidate_observed_limiter") is True or (
                manifest.get("candidate") == "codex_sdk"
                and row.get("candidate_observed_rate_limit_status") is True
                and manifest.get("mock_only_internal_network") is True
                and row.get("budget") == [{"cap": 2}]
                and len(row.get("upstream", [])) == 2
                and all(call.get("status") == 503 for call in row["upstream"]))
            for row in (first, second)),
        "persistent_cap_is_two": first.get("budget") == second.get("budget") == [{"cap": 2}],
        "exactly_two_reserved_calls": len(first.get("calls", [])) == len(second.get("calls", [])) == 2,
        "budget_not_reset": first.get("calls") == second.get("calls"),
        "upstream_only_two_calls": len(first.get("upstream", [])) == len(second.get("upstream", [])) == 2,
        "no_upstream_after_proxy_recreation": first.get("upstream") == second.get("upstream"),
        "both_calls_reached_mock": all(row.get("outcome") == "forwarded" and row.get("upstream_status") == 503 for row in second.get("calls", [])),
        "only_expected_mock_endpoint": all(row.get("endpoint") == ("/v1/responses" if manifest.get("candidate") == "codex_sdk" else "/v1/chat/completions") and row.get("status") == 503 for row in second.get("upstream", [])),
        "candidate_did_not_complete": all(row.get("candidate_status") in {"failed", "incomplete"} for row in (first, second)),
        "orchestration_no_errors": not manifest.get("errors"),
    }
    result = {"passed": all(checks.values()), "checks": checks,
              "candidate": manifest.get("candidate"), "real_model_called": False,
              "scope": "候选框架内部重试及持久化代理请求上限；不对暂停/恢复作结论"}
    save(run_root / "budget-probe-verdict.json", result)
    print(json.dumps(result))
    return 0 if result["passed"] else 1


def main():
    from runner.bench import environment
    environment()
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    serve = commands.add_parser("serve")
    serve.add_argument("--database", type=Path, required=True)
    serve.add_argument("--host", default="0.0.0.0")
    serve.add_argument("--port", type=int, default=8090)
    setup = commands.add_parser("prepare")
    setup.add_argument("--run-root", type=Path, required=True)
    setup.add_argument("--state-root", type=Path, required=True)
    snap = commands.add_parser("snapshot")
    for name in ("budget", "upstream", "candidate-dir", "output"):
        snap.add_argument("--" + name, type=Path, required=True)
    check = commands.add_parser("verify")
    check.add_argument("--run-root", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "serve":
        server = create_server(args.database, args.host, args.port)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    elif args.action == "prepare":
        prepare(args.run_root, args.state_root)
    elif args.action == "snapshot":
        save(args.output, snapshot(args.budget, args.upstream, args.candidate_dir))
    else:
        return verify(args.run_root)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
