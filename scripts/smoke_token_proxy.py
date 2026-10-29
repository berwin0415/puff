"""真实模型 smoke：验证 token 代理能把供应商 usage 记进台账。

这是计量工具校准，不是候选能力实验。只发一次很小的非流式请求；不打印密钥、
提示正文或响应正文。用法：

    .venv/bin/python scripts/smoke_token_proxy.py --output /absolute/path/to/smoke
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
from threading import Thread

import httpx

from execution.token_proxy import TokenProxyConfig, create_server


def load_env(path: Path) -> None:
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--model", default=None)
    parser.add_argument("--prompt", default="Reply with exactly OK.")
    parser.add_argument("--max-tokens", type=int, default=8)
    parser.add_argument("--stream", action="store_true")
    args = parser.parse_args()

    load_env(args.env_file)
    base_url = os.environ.get("MODEL_API_BASE_URL", "").strip()
    api_key = os.environ.get("MODEL_API_KEY", "").strip()
    model = (args.model or os.environ.get("MODEL_NAME", "")).strip()
    if not base_url or not api_key or not model:
        raise SystemExit("缺少 MODEL_API_BASE_URL / MODEL_API_KEY / MODEL_NAME")

    args.output.mkdir(parents=True, exist_ok=True)
    session_token = secrets.token_urlsafe(24)
    config = TokenProxyConfig(
        upstream_base_url=base_url,
        upstream_api_key=api_key,
        run_token=session_token,
        database=args.output / "tokens.sqlite",
        ledger=args.output / "ledger.jsonl",
        token_cap=1_000_000,
        call_cap=16,
        session_id="token-proxy-smoke",
        candidate="tooling",
        phase="calibration",
        timeout_seconds=60,
    )
    server = create_server(config, host="127.0.0.1", port=0)
    thread = Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    url = "http://127.0.0.1:" + str(server.server_address[1])
    result = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "kind": "token_proxy_smoke",
        "model": model,
        "base_url_host": base_url.split("/")[2] if "//" in base_url else "",
        "ok": False,
        "stream": bool(args.stream),
        "http_status": None,
        "usage": None,
        "error": None,
    }
    try:
        body = {
            "model": model,
            "messages": [{"role": "user", "content": args.prompt}],
            "max_tokens": args.max_tokens,
            "temperature": 0,
        }
        if args.stream:
            body["stream"] = True
        with httpx.Client(trust_env=False, timeout=70) as client:
            if args.stream:
                with client.stream(
                    "POST",
                    url + "/v1/chat/completions",
                    content=json.dumps(body).encode(),
                    headers={"Authorization": "Bearer " + session_token, "Content-Type": "application/json"},
                ) as response:
                    result["http_status"] = response.status_code
                    if response.status_code == 200:
                        for _ in response.iter_lines():
                            pass
            else:
                response = client.post(
                    url + "/v1/chat/completions",
                    content=json.dumps(body).encode(),
                    headers={"Authorization": "Bearer " + session_token, "Content-Type": "application/json"},
                )
                result["http_status"] = response.status_code
        if response.status_code != 200:
            result["error"] = "http_status_" + str(response.status_code)
        else:
            ledger_path = args.output / "ledger.jsonl"
            rows = [json.loads(line) for line in ledger_path.read_text().splitlines()]
            row = rows[-1]
            result["usage"] = {
                key: row.get(key) for key in (
                    "input_tokens", "output_tokens", "total_tokens",
                    "cache_read_tokens", "cache_write_tokens", "reasoning_tokens",
                )
            }
            result["request_id"] = row.get("request_id")
            result["ledger_status"] = row.get("status")
            result["ok"] = (
                row.get("status") == "reported"
                and (row.get("total_tokens") or 0) > 0
            )
            if not result["ok"]:
                result["error"] = "usage_not_reported"
    except Exception as exc:  # noqa: BLE001 - smoke 需要把失败写入证据
        result["error"] = type(exc).__name__
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
        (args.output / "smoke.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
