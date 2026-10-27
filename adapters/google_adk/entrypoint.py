"""运行官方 ADK Runner、LiteLlm 适配器和 SQLite 会话服务。"""

import argparse
import asyncio
from contextlib import aclosing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import signal
import sys
import time

from verify_source import verify_source


SECRETS: list[str] = []


def trusted_path(value, *, directory=False) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or any(part == ".." or part.lower() == "old" for part in value.split("/")):
        raise ValueError("可信运行路径无效")
    path, current = Path(value), Path("/")
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("可信运行路径不得跟随符号链接")
    if directory:
        path.mkdir(parents=True, exist_ok=True)
    if path.resolve(strict=True) != path:
        raise ValueError("可信运行路径解析结果发生变化")
    return path


def read_token(value) -> str:
    path = trusted_path(value)
    if not path.is_file() or path.stat().st_size > 8192:
        raise ValueError("必须提供大小受限的本地令牌文件")
    token = path.read_text().strip()
    if not token or re.search(r"\s", token):
        raise ValueError("本地令牌不能为空")
    SECRETS.append(token)
    return token


def redact(value: str) -> str:
    for token in SECRETS:
        value = value.replace(token, "[redacted-local-token]")
    return value


def encoded(value, *, pretty=False) -> str:
    def convert(item):
        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if isinstance(item, (set, frozenset)):
            return list(item)
        if hasattr(item, "value"):
            return item.value
        raise TypeError(f"不支持的原生值：{type(item).__name__}")
    return redact(json.dumps(value, default=convert, ensure_ascii=False, indent=2 if pretty else None)) + "\n"


def read_request() -> dict:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    request = json.loads(trusted_path(args.request).read_bytes())
    keys = {"run_id", "session_id", "prompt", "workspace", "state_dir", "output_dir", "tools_url", "tool_token_file", "max_output_tokens", "timeout_seconds", "max_retries"}
    if not isinstance(request, dict) or not keys <= set(request) or set(request) - keys - {"candidate_options"}:
        raise ValueError("可信运行请求字段无效")
    if "candidate_options" in request:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from detailed_support import validate_options
        validate_options(request["candidate_options"])
    for name in ("run_id", "session_id"):
        if not isinstance(request[name], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request[name]) or request[name].lower() == "old":
            raise ValueError("运行或会话标识无效")
    if not isinstance(request["prompt"], str) or not 0 < len(request["prompt"]) <= 65536:
        raise ValueError("必须提供长度受限的任务提示词")
    for name, minimum, maximum in (("max_output_tokens", 1, 16384), ("timeout_seconds", 1, 3600), ("max_retries", 0, 2)):
        if type(request[name]) is not int or not minimum <= request[name] <= maximum:
            raise ValueError("运行预算无效")
    paths = [trusted_path(request[name], directory=True) for name in ("workspace", "state_dir", "output_dir")]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for index, a in enumerate(paths) for b in paths[index + 1:]):
        raise ValueError("工作区、状态和证据必须使用相互独立的目录树")
    trusted_path(request["tool_token_file"])
    if request["tools_url"] != "http://tools:8081":
        raise ValueError("必须使用限定作用域的实验工具服务")
    return request


async def main() -> int:
    provenance = verify_source()
    request = read_request()
    if os.environ.get("MODEL_API_BASE_URL") != "http://model-proxy:8082/v1" or not os.environ.get("MODEL_NAME"):
        raise ValueError("候选必须使用计量代理和选定模型")
    token = os.environ.get("MODEL_API_KEY")
    if token:
        SECRETS.append(token)
    else:
        token = read_token(os.environ.get("MODEL_RUN_TOKEN_FILE"))
    # 浏览器用例不挂 HTTP 业务工具，因此不需要工具令牌。
    browser_case = bool(request.get("candidate_options", {}).get("case") == "browser")
    tool_token = None if browser_case else read_token(request["tool_token_file"])
    # 此路线不需要 Google 托管运行时或远程模型目录。
    os.environ["LITELLM_TELEMETRY"] = "false"
    os.environ["LITELLM_LOCAL_MODEL_COST_MAP"] = "true"
    os.environ["DO_NOT_TRACK"] = "1"
    os.environ["OTEL_SDK_DISABLED"] = "true"
    import httpx
    import litellm

    # 仅使用 SDK 传输层的公开重试设置；若同时启用 LiteLLM 独立的
    # 包装层重试设置，尝试次数可能成倍增加。
    litellm.telemetry = False
    litellm.num_retries = 0
    from google.adk.agents import Agent
    from google.adk.agents.run_config import RunConfig, StreamingMode
    from google.adk.models.lite_llm import LiteLlm
    from google.adk.runners import Runner
    from google.adk.sessions.sqlite_session_service import SqliteSessionService
    from google.genai import types
    from http_tools import load_tools

    started = time.monotonic()
    output = Path(request["output_dir"])
    options = request.get("candidate_options")
    extension_case = bool(options and options["case"] == "extension")
    browser_case = bool(options and options["case"] == "browser")
    scope = evidence = None
    observations = []
    bridge = None
    if options:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from detailed_support import Evidence, read_credentials, read_scope
        scope = read_scope(options["trusted_scope_file"], request["session_id"])
        evidence = Evidence(output, lambda value: encoded(value), scope)
        if browser_case:
            credentials = read_credentials(options["browser"]["credentials_file"], scope["user_id"])
            SECRETS.append(credentials["password"])
            request["prompt"] = (request["prompt"]
                                 + f"\n\n本轮测试登录资料：站点 {credentials['url']}，"
                                   f"用户名 {credentials['username']}，密码 {credentials['password']}。")
    raw = (output / "raw-events.jsonl").open("w", encoding="utf-8", buffering=1)
    app_name, user_id, agent_name = "harness_lab_google_adk", "screening", "lab_agent"
    sessions = SqliteSessionService(db_path=str(Path(request["state_dir"]) / "sessions.sqlite"))
    runner = None
    abort = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, abort.set)
    timeout = asyncio.timeout(request["timeout_seconds"])
    summary = {"status": "failed", "stop_reason": "runtime_error", "session_id": request["session_id"]}
    last_model_event, last_final, invocation_id = None, None, None
    native_errors = []
    iterator_exhausted = False
    try:
        async with timeout:
            if await sessions.get_session(app_name=app_name, user_id=user_id, session_id=request["session_id"]) is not None:
                raise ValueError("此初筛入口仅创建新会话；尚未实现调用恢复")
            await sessions.create_session(app_name=app_name, user_id=user_id, session_id=request["session_id"])
            async with httpx.AsyncClient(
                base_url=request["tools_url"], headers={"Authorization": f"Bearer {tool_token}"},
                timeout=request["timeout_seconds"], trust_env=False,
                transport=httpx.AsyncHTTPTransport(retries=0),
            ) as client:
                if browser_case:
                    from browser_tools import BrowserBridge
                    bridge = BrowserBridge(options["browser"], request["workspace"], evidence,
                                           request["timeout_seconds"])
                    tools = await bridge.open()
                else:
                    tools = await load_tools(client)
                if extension_case:
                    from business_extension import QuantityRuleTool, QueryReceiptTool
                    definitions = {tool.name: tool.schema for tool in tools}
                    limit = options["business_extension"]["quantity_limit"]
                    tools = [
                        QuantityRuleTool({"name": "write", "description": "提交一次业务写入（含写入前数量规则）。",
                                          "input_schema": definitions["write"]},
                                         client, limit, evidence, scope)
                        if tool.name == "write" else tool
                        for tool in tools
                    ]
                    tools.append(QueryReceiptTool(client, definitions["receipt"], evidence, scope, observations))
                model = LiteLlm(
                    model="openai/" + os.environ["MODEL_NAME"],
                    api_base=os.environ["MODEL_API_BASE_URL"], api_key=token,
                    max_tokens=request["max_output_tokens"],
                    max_retries=request["max_retries"],
                    timeout=float(request["timeout_seconds"]),
                )
                agent = Agent(
                    name=agent_name, model=model, tools=tools,
                    instruction="使用提供的工具完成用户任务。工具输入和输出来自共享实验服务。",
                    generate_content_config=types.GenerateContentConfig(max_output_tokens=request["max_output_tokens"]),
                )
                runner = Runner(app_name=app_name, agent=agent, session_service=sessions)
                (output / "entrypoint.json").write_text(encoded({
                    "run_id": request["run_id"], "session_id": request["session_id"],
                    "started_at": datetime.now(timezone.utc).isoformat(),
                    "factory": "google.adk.runners.Runner", "model_adapter": "google.adk.models.lite_llm.LiteLlm",
                    "model": model.model, "provenance": provenance,
                    "tool_names": [tool.name for tool in tools],
                    "session_service": "google.adk.sessions.sqlite_session_service.SqliteSessionService",
                    "session_scope": {"app_name": app_name, "user_id": user_id, "session_id": request["session_id"], "user_id_binding": "fixed_screening_identity_inside_trusted_per_run_state_directory"},
                    "controls": {"pause": "not_implemented", "resume": "not_verified", "cancel": "Runner_abort_signal_plus_active_timeout"},
                    "instance_boundary": "Runner_process_with_sessions; concurrent_sharing_and_tenant_binding_unverified",
                    "dependency_selection": "ADK_base_plus_selected_LiteLLM_dependency;_full_extensions_extra_not_installed",
                    "native_model_calls_limit": 20,
                    "retry_configuration": {"openai_transport_max_retries": request["max_retries"], "litellm_wrapper_num_retries": 0},
                }, pretty=True))
                async with aclosing(runner.run_async(
                    user_id=user_id, session_id=request["session_id"],
                    new_message=types.Content(role="user", parts=[types.Part(text=request["prompt"])]),
                    run_config=RunConfig(streaming_mode=StreamingMode.SSE, max_llm_calls=20),
                    abort_signal=abort,
                )) as stream:
                    async for event in stream:
                        raw.write(encoded(event))
                        invocation_id = event.invocation_id or invocation_id
                        if event.error_code or event.error_message or event.interrupted:
                            native_errors.append({"event_id": event.id, "error_code": event.error_code, "error_message": event.error_message, "interrupted": event.interrupted})
                        if event.author == agent_name and event.content and event.content.role == "model" and not event.partial:
                            last_model_event = event
                            if event.is_final_response():
                                last_final = event
                iterator_exhausted = True
                finish = getattr(last_model_event.finish_reason, "value", last_model_event.finish_reason) if last_model_event else None
                completed = iterator_exhausted and last_final is last_model_event and last_final is not None and finish == "STOP" and not native_errors and not abort.is_set()
                summary.update({
                    "status": "completed" if completed else "interrupted" if abort.is_set() else "failed" if native_errors else "incomplete",
                    "stop_reason": finish or "missing_native_model_terminal",
                    "final_response": "".join(part.text for part in last_final.content.parts or [] if part.text and not part.thought) if last_final and last_final.content else None,
                    "native_result": last_model_event,
                })
    except Exception as error:
        is_timeout = isinstance(error, TimeoutError) or timeout.expired()
        summary.update({
            "status": "timed_out" if is_timeout else "interrupted" if abort.is_set() else "failed",
            "stop_reason": "active_timeout" if is_timeout else "process_cancellation" if abort.is_set() else "runtime_error",
            "error_type": type(error).__name__, "error": str(error),
        })
    finally:
        raw.close()
        for signum in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(signum)
        cleanup_errors = []
        try:
            snapshot = await sessions.get_session(app_name=app_name, user_id=user_id, session_id=request["session_id"])
            payload = json.loads(encoded(snapshot, pretty=True))
            if extension_case:
                from business_extension import aggregate_business_state
                payload["harness_lab_business_state"] = aggregate_business_state(scope, observations)
            (output / "state-snapshot.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
            if extension_case:
                data = (output / "state-snapshot.json").read_bytes()
                import hashlib
                evidence.save("business-state.json", {
                    "scope": scope,
                    "source": {"path": "state-snapshot.json",
                               "sha256": hashlib.sha256(data).hexdigest(),
                               "json_pointer": "/harness_lab_business_state"},
                    "business_state": payload["harness_lab_business_state"],
                })
                evidence.finalize()
        except Exception as error:
            cleanup_errors.append({"operation": "session_snapshot", "error_type": type(error).__name__, "error": str(error)})
        if runner:
            try:
                await runner.close()
            except Exception as error:
                cleanup_errors.append({"operation": "runner_close", "error_type": type(error).__name__, "error": str(error)})
        try:
            await sessions.close()
        except Exception as error:
            cleanup_errors.append({"operation": "session_service_close", "error_type": type(error).__name__, "error": str(error)})
        if bridge is not None:
            try:
                await bridge.close()
            except Exception as error:
                cleanup_errors.append({"operation": "browser_mcp_close", "error_type": type(error).__name__, "error": str(error)})
        if cleanup_errors:
            summary["cleanup_errors"] = cleanup_errors
            if summary["status"] == "completed":
                summary.update({"status": "failed", "stop_reason": "evidence_or_cleanup_failure"})
    finish = getattr(last_model_event.finish_reason, "value", last_model_event.finish_reason) if last_model_event else None
    summary["native_terminal"] = {"iterator_exhausted": iterator_exhausted, "invocation_id": invocation_id, "observed_final_response": last_final is not None, "last_model_finish_reason": finish, "errors": native_errors}
    summary["elapsed_seconds"] = time.monotonic() - started
    (output / "result.json").write_text(encoded(summary, pretty=True))
    print(encoded({"status": summary["status"], "stop_reason": summary["stop_reason"], "session_id": request["session_id"]}), end="")
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        print(redact(f"{type(error).__name__}: {error}"), file=sys.stderr)
        raise SystemExit(1) from None
