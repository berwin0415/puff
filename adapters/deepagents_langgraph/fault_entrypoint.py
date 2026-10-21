"""Separate I23 tools runtime; natural SQLite crash recovery, no pause hooks.

A and B use this identical image/graph. Only B's input changes to None. No
model/tool call, state repair or synthetic checkpoint occurs before native run.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import os
from pathlib import Path
import time

from entrypoint import BUILTINS, encoded, read_token, redact, trusted_path
from detailed_support import Evidence, completion, read_scope
from fault_recovery import CrashRecovery, validate_request
from verify_source import verify_source


def request_from_file():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    from pause_recovery import _json, _read_file
    request = _json(_read_file(trusted_path(args.request), 65536))
    if not isinstance(request, dict):
        raise ValueError("I23 request must be a JSON object")
    control = validate_request(request)
    fields = {"run_id", "session_id", "workspace", "state_dir", "output_dir", "tools_url", "tool_token_file",
        "max_output_tokens", "timeout_seconds", "max_retries", "candidate_options", "fault_recovery"}
    if control["mode"] == "new":
        fields.add("prompt")
    if set(request) != fields:
        raise ValueError("Unexpected I23 run fields; resume input must remain None")
    from detailed_support import IDENTIFIER
    if any(not isinstance(request[key], str) or not IDENTIFIER.fullmatch(request[key]) for key in ("run_id", "session_id")):
        raise ValueError("Invalid I23 original identity")
    if control["mode"] == "new" and (not isinstance(request["prompt"], str) or not 0 < len(request["prompt"]) <= 65536):
        raise ValueError("Invalid original I23 objective")
    for key, expected in {"workspace": "/workspace", "state_dir": "/state", "output_dir": "/evidence",
            "tool_token_file": "/config/scope.token", "tools_url": "http://tools:8081"}.items():
        if request[key] != expected:
            raise ValueError("I23 fixed mount/service identity changed")
        if key != "tools_url":
            trusted_path(expected, directory=key in {"workspace", "state_dir", "output_dir"})
    for key, maximum in (("max_output_tokens", 16384), ("timeout_seconds", 300), ("max_retries", 2)):
        if type(request[key]) is not int or not (0 if key == "max_retries" else 1) <= request[key] <= maximum:
            raise ValueError("I23 invalid request budget")
    return request


async def main():
    started = time.monotonic()
    provenance = verify_source()
    request = request_from_file()
    scope = read_scope("/config/scope.json", request["session_id"])
    if (os.environ.get("MODEL_API_BASE_URL") != "http://model-proxy:8082/v1" or not os.environ.get("MODEL_NAME")
            or os.environ.get("MODEL_RUN_TOKEN_FILE") != "/budget/run.token" or os.environ.get("MODEL_API_KEY")):
        raise ValueError("I23 requires the original counted model token")
    token, tool_token = read_token("/budget/run.token"), read_token("/config/scope.token")
    lifecycle = CrashRecovery(request, scope, provenance, token, started)
    output = Path(request["output_dir"])
    evidence = Evidence(output, encoded, scope)
    tools = None
    summary = {"status": "failed", "native_status": "not_started", "scope": scope,
        "run_id": request["run_id"], "session_id": request["session_id"], "stop_reason": "runtime_error"}
    try:
        from deepagents import GeneralPurposeSubagentProfile, HarnessProfile, create_deep_agent, register_harness_profile
        from langchain_core.messages import AIMessage
        from langchain_openai import ChatOpenAI
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
        from http_tools import HttpTools
        from pause_recovery import TOOLS_SYSTEM_PROMPT

        model_name = os.environ["MODEL_NAME"]
        register_harness_profile("openai:" + model_name, HarnessProfile(excluded_tools=BUILTINS,
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False)))
        model = ChatOpenAI(model=model_name, base_url=os.environ["MODEL_API_BASE_URL"], api_key=token,
            use_responses_api=False, max_tokens=request["max_output_tokens"], max_retries=request["max_retries"],
            timeout=request["timeout_seconds"])
        def observe_http(event):
            return evidence.append("http-events.jsonl", {"time": evidence.now(), "scope": scope, **event})
        tools = HttpTools(request["tools_url"], tool_token, request["timeout_seconds"],
            on_event=observe_http, evidence=evidence, scope=scope)
        # Fetching tool definitions has no model or business write side effect.
        async with asyncio.timeout(lifecycle.remaining()):
            provided = await tools.load()
        definitions = [{"name": tool.name.removeprefix("lab_"), "description": tool.description,
            "input_schema": tool.args_schema} for tool in provided]
        runtime_hash = lifecycle.runtime(definitions)
        evidence.save("entrypoint.json", {"scope": scope, "run_id": request["run_id"], "session_id": request["session_id"],
            "started_at": datetime.now(timezone.utc).isoformat(), "provenance": provenance, "variant": "fault-recovery",
            "factory": "deepagents.create_deep_agent", "runtime_sha256": runtime_hash,
            "fault_recovery": request["fault_recovery"], "durability": "sync", "pause_hooks_installed": False,
            "source_of_execution_state": "original /state/checkpoints.sqlite including natural WAL and pending writes"})
        async with AsyncSqliteSaver.from_conn_string(str(Path(request["state_dir"]) / "checkpoints.sqlite")) as saver:
            graph = create_deep_agent(model=model, tools=provided, system_prompt=TOOLS_SYSTEM_PROMPT,
                checkpointer=saver, interrupt_on=None)
            config = lifecycle.config()
            if lifecycle.control["mode"] == "crash-resume":
                restored = await lifecycle.check_resume(graph, saver)
                evidence.save("native-crash-resume.json", restored)
                initial = None
            else:
                previous = await graph.aget_state(config)
                if previous.values:
                    raise ValueError("New I23 task must not reuse native progress")
                initial = {"messages": [{"role": "user", "content": request["prompt"]}]}
            async with asyncio.timeout(lifecycle.remaining()):
                async for mode, event in graph.astream(initial, config=config,
                        stream_mode=["updates", "messages", "tasks", "checkpoints"], durability="sync"):
                    evidence.append("raw-events.jsonl", {"time": evidence.now(), "scope": scope,
                        "run_id": request["run_id"], "session_id": request["session_id"], "stream_mode": mode, "data": event})
            snapshot = await graph.aget_state(config)
            evidence.save("state-snapshot.json", snapshot._asdict())
            messages = snapshot.values.get("messages", [])
            last = messages[-1] if messages else None
            native = completion(last_is_ai=isinstance(last, AIMessage), tool_calls=getattr(last, "tool_calls", []),
                invalid_tool_calls=getattr(last, "invalid_tool_calls", []), metadata=getattr(last, "response_metadata", {}),
                pending=snapshot.next, task_errors=[task.error for task in snapshot.tasks if task.error],
                interrupts=[interrupt for task in snapshot.tasks for interrupt in task.interrupts])
            summary.update({"status": native["native_status"], **native, "native_result": snapshot.values,
                "final_response": last.content if isinstance(last, AIMessage) else None,
                "checkpoint_id": snapshot.config.get("configurable", {}).get("checkpoint_id"),
                "native_terminal": {"finish_reason": native["finish_reason"], "response_metadata": native["response_metadata"],
                    "pending_tool_calls": bool(getattr(last, "tool_calls", []) or getattr(last, "invalid_tool_calls", [])),
                    "is_streaming": False, "error_message": "native_task_error" if native["native_status"] == "failed" else None}})
    except Exception as error:
        summary.update({"status": "timed_out" if isinstance(error, TimeoutError) else "failed",
            "native_status": "timed_out" if isinstance(error, TimeoutError) else "failed",
            "stop_reason": "active_timeout" if isinstance(error, TimeoutError) else "runtime_error",
            "error_type": type(error).__name__, "error": str(error)})
    finally:
        try:
            if tools is not None:
                await tools.close()
            evidence.finalize()
        finally:
            lifecycle.close()
    summary["elapsed_seconds"] = time.monotonic() - started
    evidence.save("result.json", summary)
    return 0 if summary["status"] == "completed" else 1


if __name__ == "__main__":
    import sys
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as error:
        print(redact(f"{type(error).__name__}: {error}"), file=sys.stderr)
        raise SystemExit(1) from None
