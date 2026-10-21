"""Run the fixed public Agent/Runner and Chat Completions provider inside Docker."""

import argparse
import asyncio
from dataclasses import fields, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import json
import hashlib
import os
from pathlib import Path
import re
import signal
import sys
import time

from verify_source import verify_source
from detailed_support import Evidence, existing_path, read_credentials, read_scope, secret_forms, validate_options


SECRETS: list[str] = []


def trusted_path(value, *, directory=False) -> Path:
    if not isinstance(value, str) or not value.startswith("/") or any(part == ".." or part.lower() == "old" for part in value.split("/")):
        raise ValueError("Invalid trusted run path")
    path, current = Path(value), Path("/")
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("Trusted run paths must not follow symbolic links")
    if directory:
        path.mkdir(parents=True, exist_ok=True)
    if path.resolve(strict=True) != path:
        raise ValueError("Trusted run path resolution changed")
    return path


def read_token(value) -> str:
    path = trusted_path(value)
    if not path.is_file() or path.stat().st_size > 8192:
        raise ValueError("Expected a bounded local token file")
    token = path.read_text().strip()
    if not token or re.search(r"\s", token):
        raise ValueError("Expected a nonempty local token")
    SECRETS.append(token)
    return token


def redact(value: str) -> str:
    for token in secret_forms(SECRETS):
        value = value.replace(token, "[redacted-local-token]")
    return value


def native_json(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, Path)):
        return str(value)
    if isinstance(value, (set, frozenset)):
        return list(value)
    if isinstance(value, BaseException):
        return {"exception_type": type(value).__name__, "message": str(value)}
    if type(value).__module__ == "agents.agent" and hasattr(value, "name"):
        # Agent definitions retain live Python callables and the model client.
        # Events need their identity, not a dump of clients/credentials.
        return {"agent_name": value.name}
    if is_dataclass(value):
        return {item.name: getattr(value, item.name) for item in fields(value) if not item.name.startswith("_")}
    return {"python_type": f"{type(value).__module__}.{type(value).__name__}", "value": "not_json_serializable"}


def encoded(value, *, pretty=False) -> str:
    return redact(json.dumps(value, default=native_json, ensure_ascii=False, indent=2 if pretty else None)) + "\n"


def read_request():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--validate-only", action="store_true")
    modes.add_argument("--restore-business-state-only", action="store_true")
    args = parser.parse_args()
    request = json.loads(trusted_path(args.request).read_bytes())
    keys = {"run_id", "session_id", "prompt", "workspace", "state_dir", "output_dir", "tools_url", "tool_token_file", "max_output_tokens", "timeout_seconds", "max_retries"}
    if not isinstance(request, dict) or not keys <= set(request) or set(request) - keys - {"candidate_options"}:
        raise ValueError("Invalid trusted run request fields")
    for name in ("run_id", "session_id"):
        if not isinstance(request[name], str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", request[name]) or request[name].lower() == "old":
            raise ValueError("Invalid run/session identifier")
    if not isinstance(request["prompt"], str) or not 0 < len(request["prompt"]) <= 65536:
        raise ValueError("Expected a bounded task prompt")
    for name, minimum, maximum in (("max_output_tokens", 1, 16384), ("timeout_seconds", 1, 3600), ("max_retries", 0, 2)):
        if type(request[name]) is not int or not minimum <= request[name] <= maximum:
            raise ValueError("Invalid run budget")
    paths = [trusted_path(request[name], directory=True) for name in ("workspace", "state_dir", "output_dir")]
    if any(a.is_relative_to(b) or b.is_relative_to(a) for index, a in enumerate(paths) for b in paths[index + 1:]):
        raise ValueError("State, workspace and evidence must use separate directory trees")
    trusted_path(request["tool_token_file"])
    if request["tools_url"] != "http://tools:8081":
        raise ValueError("Expected the scoped experiment tools service")
    if "candidate_options" in request:
        validate_options(request["candidate_options"])
    if args.restore_business_state_only and request.get("candidate_options", {}).get("case") != "extension":
        raise ValueError("Business state restoration is only available for the extension case")
    return request, args.validate_only, args.restore_business_state_only


async def stop_native(result, emit) -> None:
    if result is None:
        return
    result.cancel(mode="immediate")
    # The public cancellation contract requires consuming the stream afterwards.
    try:
        async with asyncio.timeout(5):
            async for event in result.stream_events():
                emit("native_cleanup_event", event)
    except (Exception, asyncio.CancelledError) as error:
        emit("native_cleanup_error", {"error_type": type(error).__name__, "error": str(error)})


def make_agent(definitions, model, request, browser_case=False):
    """One public Agent definition shared by running and offline restoration."""
    from agents import Agent, ModelSettings

    settings = ModelSettings(max_tokens=request["max_output_tokens"], include_usage=True, preserve_raw_usage=True)
    instructions = ("Use the provided browser primitives to carry out the user's task. Do not disclose authentication credentials."
        if browser_case else "Use the provided experiment tools to carry out the user's task. Tool definitions and their returned data are authoritative.")
    return Agent(name="harness-lab-agent", instructions=instructions, model=model, model_settings=settings, tools=definitions)


async def inspect_business_state(request, scope, extension, provenance, model_name, token):
    """Rebuild static tools and native RunState with no discovery, model or session I/O."""
    from agents import OpenAIChatCompletionsModel
    from openai import AsyncOpenAI
    import httpx2
    from business_extension import read_business_state, restore_business_state
    from http_tools import HttpTools
    from tool_definitions import load_authenticated_definitions, read_bounded

    output, state = Path(request["output_dir"]), Path(request["state_dir"])
    if any(output.iterdir()):
        raise ValueError("Business state inspection requires a fresh empty evidence directory")
    started = time.monotonic()
    path = trusted_path(str(state / "run-state.json"))
    serialized = read_bounded(path, 16 * 1024 * 1024)
    native_state = json.loads(serialized)
    original_business = read_business_state(native_state, scope)
    native_context = native_state["context"]["context"]
    binding = native_context.get("tool_definition_cache")
    expected = {"run_id": request["run_id"], "session_id": request["session_id"], "scope": scope,
        "tool_definition_cache": binding}
    if set(native_context) != set(expected) | {"business_state"} or any(native_context.get(key) != value for key, value in expected.items()):
        raise ValueError("Native SDK context has a different trusted run identity")
    definitions, cache_source = load_authenticated_definitions(state, expected, provenance, binding)

    class RejectNetwork(httpx2.AsyncBaseTransport):
        def __init__(self):
            self.attempts = []

        async def handle_async_request(self, outgoing):
            self.attempts.append({"method": outgoing.method, "path": outgoing.url.path})
            raise RuntimeError("Offline native state inspection attempted a model HTTP request")

    transport = RejectNetwork()
    http_client = httpx2.AsyncClient(transport=transport, trust_env=False, follow_redirects=False)
    client = AsyncOpenAI(base_url=os.environ["MODEL_API_BASE_URL"], api_key=token, max_retries=0, http_client=http_client)
    tools = HttpTools(request["tools_url"], None, request["timeout_seconds"], expected, extension=extension, offline=True)
    summary = {"status": "failed", "stop_reason": "offline_native_state_restore_failed", "session_id": request["session_id"],
        "scope": scope, "runner_started": False, "model_calls": 0, "http_discovery_requests": 0,
        "task_continuation_verified": False}
    try:
        async with asyncio.timeout(request["timeout_seconds"]):
            provided = tools.build(definitions)
            model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
            agent = make_agent(provided, model, request)
            (output / "entrypoint.json").write_text(encoded({"run_id": request["run_id"], "session_id": request["session_id"],
                "scope": scope, "provenance": provenance, "factory": "agents.Agent + RunState.from_json; Runner not started",
                "tool_names": [tool.name for tool in provided], "tool_definition_cache": cache_source,
                "model": model_name, "model_transport": "reject every request", "mcp_servers": [],
                "session": "SQLiteSession not constructed; original state is read only"}, pretty=True))
            restored, reconstructed = await restore_business_state(agent, native_state, scope)
            if reconstructed != original_business:
                raise ValueError("Restored native business context changed")
            if restored.to_json(strict_context=True, include_tracing_api_key=False)["context"]["context"].get("tool_definition_cache") != binding:
                raise ValueError("Restored native context lost its authenticated tool definition binding")
            if transport.attempts:
                raise ValueError("Native deserialization attempted model HTTP")
            if read_bounded(path, 16 * 1024 * 1024) != serialized:
                raise ValueError("Original native RunState changed during inspection")
            # Re-read cache bindings too. Neither file is a replacement for the
            # original native RunState; they contain tool definitions only.
            load_authenticated_definitions(state, expected, provenance, binding)
            (output / "native-state-reconnected.json").write_bytes(serialized)
            source = {"path": "native-state-reconnected.json", "sha256": hashlib.sha256(serialized).hexdigest(),
                "json_pointer": "/context/context/business_state"}
            (output / "business-state-reconnected.json").write_text(encoded({"scope": scope, "source": source,
                "native_origin": {"path": str(path), "sha256": source["sha256"], "unchanged": True},
                "tool_definition_cache": cache_source, "public_api": "RunState.from_json + RunState.to_json(strict_context=True)",
                "business_state": reconstructed, "pid": os.getpid(), "runner_started": False,
                "model_calls": 0, "http_discovery_requests": 0, "tool_calls": 0,
                "task_continuation_verified": False}, pretty=True))
            summary.update({"status": "validated", "stop_reason": "native_business_state_reconstructed_offline_without_runner",
                "business_context_public_roundtrip": True, "business_state_source": source,
                "original_native_state_unchanged": True, "tool_definition_cache": cache_source})
    except Exception as error:
        summary.update({"error_type": type(error).__name__, "error": str(error)})
    finally:
        await tools.close()
        await client.close()
        summary.update({"model_client_request_attempts": transport.attempts, "elapsed_seconds": time.monotonic() - started})
        (output / "result.json").write_text(encoded(summary, pretty=True))
    print(encoded({key: summary[key] for key in ("status", "stop_reason", "session_id")}), end="")
    return 0 if summary["status"] == "validated" else 1


async def main() -> int:
    provenance = verify_source()
    request, validate_only, restore_business_only = read_request()
    model_name = os.environ.get("MODEL_NAME", "")
    if os.environ.get("MODEL_API_BASE_URL") != "http://model-proxy:8082/v1" or not model_name or len(model_name) > 512 or re.search(r"\s", model_name):
        raise ValueError("Candidate requires the fixed counting proxy and exact selected model")
    if os.environ.get("MODEL_API_KEY") or os.environ.get("OPENAI_API_KEY"):
        raise ValueError("Candidate must receive only a local model run token file")
    token = read_token(os.environ.get("MODEL_RUN_TOKEN_FILE"))
    options = request.get("candidate_options")
    scope = read_scope(options["trusted_scope_file"], request["session_id"]) if options else None
    browser_case = bool(options and options["case"] == "browser")
    extension = options["business_extension"] if options and options["case"] == "extension" else None
    credentials = read_credentials(options["browser"]["credentials_file"], scope["user_id"]) if browser_case else None
    if credentials:
        SECRETS.append(credentials["password"])
        existing_path(options["browser"]["artifacts_dir"])
    tool_token = None if browser_case else read_token(request["tool_token_file"])
    # Set before SDK import; also disable tracing globally and per-run below.
    os.environ["OPENAI_AGENTS_DISABLE_TRACING"] = "1"
    from agents import OpenAIChatCompletionsModel, RunConfig, RunHooks, Runner, SQLiteSession, set_tracing_disabled
    from openai import AsyncOpenAI
    import httpx2
    from model_transport import ObservedTransport

    set_tracing_disabled(True)
    if restore_business_only:
        return await inspect_business_state(request, scope, extension, provenance, model_name, token)
    output, state = Path(request["output_dir"]), Path(request["state_dir"])
    os.chdir(request["workspace"])
    started = time.monotonic()
    raw = (output / "raw-events.jsonl").open("w", encoding="utf-8", buffering=1)
    raw_lines = 0
    session, result = None, None

    def emit(event_type, event):
        nonlocal raw_lines
        data = encoded({"time": datetime.now(timezone.utc).isoformat(), "run_id": request["run_id"],
                        "session_id": request["session_id"], "scope": scope,
                        "native_current_turn": result.current_turn if result is not None else None,
                        "native_is_complete": result.is_complete if result is not None else None,
                        "type": event_type, "event": event})
        raw.write(data)
        raw_lines += 1
        return {"path": "raw-events.jsonl", "line": raw_lines,
                "line_sha256": hashlib.sha256(data.encode()).hexdigest()}

    class EvidenceHooks(RunHooks):
        def __init__(self):
            self.agent_ends = 0

        async def on_llm_start(self, context, agent, system_prompt, input_items):
            source = emit("llm_start", {"agent_name": agent.name, "system_prompt": system_prompt, "input_items": input_items})
            cancel_control.activity_start("llm", source)

        async def on_llm_end(self, context, agent, response):
            emit("llm_end", {"agent_name": agent.name, "response": response})
            cancel_control.activity_end("llm")

        async def on_agent_end(self, context, agent, final_output):
            self.agent_ends += 1
            emit("agent_end", {"agent_name": agent.name, "final_output": final_output})
            cancel_control.active.clear()

        async def on_tool_start(self, context, agent, tool):
            call_id = getattr(context, "tool_call_id", None)
            source = emit("tool_start", {"agent_name": agent.name, "tool_name": tool.name, "call_id": call_id, "arguments": getattr(context, "tool_arguments", None)})
            cancel_control.tool_started(source, tool.name, call_id)

        async def on_tool_end(self, context, agent, tool, result):
            emit("tool_end", {"agent_name": agent.name, "tool_name": tool.name, "call_id": getattr(context, "tool_call_id", None), "result": result})
            cancel_control.activity_end("tool", getattr(context, "tool_call_id", None) or tool.name)

    identity = {"run_id": request["run_id"], "session_id": request["session_id"]}
    if scope:
        identity["scope"] = scope
    if extension:
        from business_extension import new_business_state
        identity["business_state"] = new_business_state()
    evidence = Evidence(output, encoded, scope) if options else None
    transport = ObservedTransport(output, model_name, emit, secrets=SECRETS if browser_case else ())
    http_client = httpx2.AsyncClient(transport=transport, trust_env=False, follow_redirects=False, headers={"Accept-Encoding": "identity"}, timeout=request["timeout_seconds"])
    client = AsyncOpenAI(base_url=os.environ["MODEL_API_BASE_URL"], api_key=token, max_retries=request["max_retries"], timeout=request["timeout_seconds"], http_client=http_client)
    if browser_case:
        from browser_tools import BrowserTools
        tools = BrowserTools(options["browser"], request["workspace"], evidence, request["timeout_seconds"], identity)
    else:
        from http_tools import HttpTools
        tools = HttpTools(request["tools_url"], tool_token, request["timeout_seconds"], identity, evidence=evidence, extension=extension)
    from cancel_control import NativeCancelControl
    cancel_control = NativeCancelControl(identity, evidence, emit, lambda: result)
    hooks = EvidenceHooks()
    summary = {"status": "failed", "session_id": request["session_id"], "stop_reason": "runtime_error"}
    interrupted_by = []
    loop, main_task = asyncio.get_running_loop(), asyncio.current_task()

    def interrupt(name):
        interrupted_by.append(name)
        main_task.cancel()

    for value in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(value, interrupt, value.name)
    if options:
        loop.add_signal_handler(signal.SIGUSR1, cancel_control.request)
    try:
        async with asyncio.timeout(request["timeout_seconds"]):
            definitions = await tools.load() if browser_case else await tools.load(state_dir=state, provenance=provenance)
            session = SQLiteSession(request["session_id"], db_path=state / "sessions.sqlite")
            if await session.get_items():
                raise ValueError("This screening entry accepts new sessions only; resume is not implemented")
            model = OpenAIChatCompletionsModel(model=model_name, openai_client=client)
            agent = make_agent(definitions, model, request, browser_case)
            (output / "entrypoint.json").write_text(encoded({
                **identity, "provenance": provenance, "started_at": datetime.now(timezone.utc).isoformat(),
                "factory": "agents.Agent + agents.Runner.run_streamed",
                "model": model_name, "provider": "OpenAIChatCompletionsModel + AsyncOpenAI",
                "tool_names": [tool.name for tool in definitions], "tool_schema_policy": "original schema; strict rewriting disabled; validation by MCP server" if browser_case else "original schema; strict rewriting disabled; validation by shared HTTP service",
                "detailed_case": options["case"] if options else None,
                "business_extension": {**extension, "added_tool": "query_order_status -> receipt",
                    "state": "public mapping RunContextWrapper.context -> native RunState JSON context.context.business_state",
                    "quantity_guard": "candidate tool execution before HTTP write; no application retries or automatic receipt query"} if extension else None,
                "browser": {"transport": "official mcp.ClientSession / streamable HTTP", "business_http_tools": False,
                    "script_tools": False, "credentials": "separate trusted file delivered to actual model input; saved evidence redacted",
                    "artifact_source": "read-only /browser-artifacts; export only from actual completed download event"} if browser_case else None,
                "retry_policy": {"provider_max_retries": request["max_retries"], "runner_model_retry": "not_enabled", "transport_retries": 0, "application_tool_retries": 0},
                "session": {"class": "agents.SQLiteSession", "database": "sessions.sqlite", "purpose": "native durable conversation history"},
                "tracing": "disabled before import, globally, and per run; no OpenAI account or key",
                "controls": {"pause": "not_implemented_by_this_entry", "resume": "RunState serialization material only; not executed or verified",
                    "cancel": "detailed SIGUSR1 -> public RunResultStreaming.cancel(mode=immediate) during a live official hook; consume original stream",
                    "cancel_native_terminal": "is_complete is not natural success; SDK has no DSH idle/aborted event"},
                "instance_boundary": "one application process for initial screening; SDK objects do not imply one container per user",
            }, pretty=True))
            if validate_only:
                summary.update({"status": "validated", "stop_reason": "validation_only_no_model_request"})
            else:
                task_input = request["prompt"]
                if browser_case:
                    task_input = [{"role": "user", "content": request["prompt"]},
                        {"role": "user", "content": "Trusted test login credentials (keep confidential): " + json.dumps(credentials, ensure_ascii=False)}]
                result = Runner.run_streamed(agent, task_input, context=identity, session=session, hooks=hooks, max_turns=20, run_config=RunConfig(model=model, tracing_disabled=True))
                cancel_control.input_submitted = True
                async for event in result.stream_events():
                    emit("native_stream_event", event)
                settlement = cancel_control.stream_closed(hooks.agent_ends)
                last_wire = transport.records[-1] if transport.records else {}
                native_done = result.is_complete and result.run_loop_exception is None and not result.interruptions and result.final_output is not None and bool(result.raw_responses) and hooks.agent_ends > 0
                wire_done = last_wire.get("status") == 200 and last_wire.get("sse") and last_wire.get("finish_reason") == "stop" and last_wire.get("saw_done") and not last_wire.get("observation_error") and not last_wire.get("provider_error")
                completed = bool(native_done and wire_done)
                summary.update({
                    "status": "cancelled" if settlement["confirmed"] else "completed" if completed else "incomplete",
                    "stop_reason": "native_immediate_cancel_stream_exhausted" if settlement["confirmed"] else "native_final_output" if completed else "unconfirmed_native_or_provider_terminal",
                    "final_response": result.final_output,
                    "native_terminal": settlement["native_terminal"], "native_terminal_source": settlement["source"],
                    "stream_exhausted": True,
                    "experiment_cancel": {"requested": cancel_control.request_record is not None,
                        "api_executed": cancel_control.api_executed, "confirmed": settlement["confirmed"],
                        "source": settlement["control_source"], "native_status_enum": "SDK does not provide a cancelled status enum"},
                    "native_responses": result.raw_responses, "native_items": result.new_items,
                })
    except asyncio.CancelledError:
        summary.update({"status": "interrupted", "stop_reason": "container_signal", "interrupted_by": interrupted_by})
        await stop_native(result, emit)
    except Exception as error:
        summary.update({"status": "timed_out" if isinstance(error, TimeoutError) else "failed", "stop_reason": "active_timeout" if isinstance(error, TimeoutError) else "runtime_error", "error_type": type(error).__name__, "error": str(error)})
        await stop_native(result, emit)
    finally:
        for value in (signal.SIGTERM, signal.SIGINT):
            loop.remove_signal_handler(value)
        if options:
            loop.remove_signal_handler(signal.SIGUSR1)
        if result is not None:
            try:
                native_state = result.to_state().to_json(strict_context=bool(extension), include_tracing_api_key=False)
                data = encoded(native_state, pretty=True)
                (state / "run-state.json").write_text(data)
                (output / "native-state.json").write_text(data)
                summary["state_snapshot"] = "native RunState JSON; diagnostic material, resume not verified"
                if extension:
                    from business_extension import read_business_state, restore_business_state
                    business = read_business_state(native_state, scope)
                    source = {"path": "native-state.json", "sha256": hashlib.sha256(data.encode()).hexdigest(),
                              "json_pointer": "/context/context/business_state"}
                    evidence.save("business-state.json", {"scope": scope, "source": source,
                        "business_state": business, "kind": "read-only diagnostic projection of native RunState; not a checkpoint"})
                    summary["business_state_source"] = source
                    try:
                        _, reconstructed = await restore_business_state(agent, native_state, scope)
                        if reconstructed != business:
                            raise ValueError("Public SDK restoration changed business context")
                        evidence.save("business-state-roundtrip.json", {"scope": scope, "source": source,
                            "public_api": "RunState.from_json + RunState.to_json(strict_context=True)",
                            "business_state": reconstructed, "same_process_only": True,
                            "cross_container_continuation_verified": False})
                        summary["business_context_public_roundtrip"] = True
                    except Exception as error:
                        summary["business_context_public_roundtrip"] = False
                        summary["business_context_restore_error"] = {"error_type": type(error).__name__, "error": str(error)}
            except Exception as error:
                summary["state_snapshot_error"] = {"error_type": type(error).__name__, "error": str(error)}
        if session is not None:
            try:
                history = await session.get_items()
                (output / "session-history.json").write_text(encoded(history, pretty=True))
                summary["persisted_history_items"] = len(history)
            except Exception as error:
                summary["history_snapshot_error"] = {"error_type": type(error).__name__, "error": str(error)}
            finally:
                try:
                    session.close()
                except Exception as error:
                    summary.setdefault("cleanup_errors", []).append({"resource": "sqlite_session", "error_type": type(error).__name__, "error": str(error)})
        for resource, close in (("browser_mcp" if browser_case else "http_tools", tools.close), ("model_client", client.close)):
            try:
                async with asyncio.timeout(5):
                    await close()
            except (Exception, asyncio.CancelledError) as error:
                summary.setdefault("cleanup_errors", []).append({"resource": resource, "error_type": type(error).__name__, "error": str(error)})
        summary["provider_http_attempts"] = transport.records
        if options:
            summary["scope"] = scope
            summary["execution_denial_observed"] = tools.denied
            summary["experiment_policy_status"] = ("denied" if tools.denied and summary["status"] == "completed"
                else "execution_denial_observed_but_run_incomplete" if tools.denied else "no_execution_denial_observed")
            if browser_case:
                summary["download_export"] = tools.download
            if extension:
                summary["business_rule_denial_observed"] = tools.quantity_denied
            try:
                evidence.finalize()
            except Exception as error:
                summary["evidence_index_error"] = {"error_type": type(error).__name__, "error": str(error)}
        summary["elapsed_seconds"] = time.monotonic() - started
        (output / "result.json").write_text(encoded(summary, pretty=True))
        raw.close()
    print(encoded({"status": summary["status"], "stop_reason": summary["stop_reason"], "session_id": request["session_id"]}), end="")
    return 0 if summary["status"] in {"completed", "validated"} else 1


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        print(redact(f"{type(error).__name__}: {error}"), file=sys.stderr)
        raise SystemExit(1) from None
