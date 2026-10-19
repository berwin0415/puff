"""External worker used by the Deep Agents + LangGraph adapter.

The worker is deliberately a small JSONL process.  Candidate dependencies are
installed in an external virtual environment, while this file remains in the
validation repository.  One request runs one graph invocation and emits only
structured event summaries; prompts, credentials and full model responses are
never written to the event stream by default.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _message_summary(message: Any) -> dict[str, Any]:
    content = getattr(message, "content", "")
    if isinstance(content, list):
        content_text = "".join(str(part.get("text", "")) if isinstance(part, dict) else str(part) for part in content)
    else:
        content_text = str(content)
    summary: dict[str, Any] = {
        "type": type(message).__name__,
        "content_preview": content_text[:500],
    }
    name = getattr(message, "name", None)
    if name:
        summary["name"] = name
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        summary["tool_calls"] = [
            {"name": call.get("name"), "id": call.get("id")} for call in tool_calls if isinstance(call, dict)
        ]
    usage = getattr(message, "usage_metadata", None)
    if isinstance(usage, dict):
        summary["usage"] = {
            key: usage.get(key)
            for key in ("input_tokens", "output_tokens", "total_tokens")
            if usage.get(key) is not None
        }
    return summary


def run(request: dict[str, Any]) -> int:
    # Imports are intentionally inside the worker entrypoint.  The parent
    # process does not need candidate packages installed in the repository venv.
    from deepagents import create_deep_agent
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.sqlite import SqliteSaver

    @tool
    def fixture_lookup(key: str) -> str:
        """Return a deterministic value from the local fixture set."""

        values = {"alpha": "fixture-alpha", "browser_title": "Harness Fixture"}
        return values.get(key, f"missing:{key}")

    model_name = os.environ.get("MODEL_NAME", "")
    base_url = os.environ.get("MODEL_API_BASE_URL", "")
    api_key = os.environ.get("MODEL_API_KEY", "")
    if not model_name or not base_url or not api_key:
        raise RuntimeError("MODEL_NAME, MODEL_API_BASE_URL and MODEL_API_KEY are required")

    run_id = str(request.get("run_id") or "")
    prompt = request.get("prompt")
    if not run_id or not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("request requires run_id and a non-empty prompt")
    checkpoint_db = Path(str(request.get("checkpoint_db") or "langgraph-checkpoints.sqlite")).expanduser()
    checkpoint_db.parent.mkdir(parents=True, exist_ok=True)

    llm = ChatOpenAI(
        model=model_name,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        max_tokens=int(request.get("max_tokens") or 256),
        timeout=float(request.get("timeout_seconds") or 120),
        max_retries=0,
    )

    def emit(event_type: str, payload: dict[str, Any] | None = None) -> None:
        print(_json({"event_type": event_type, "payload": payload or {}}), flush=True)

    emit("graph_started", {"thread_id": run_id, "checkpoint_backend": "sqlite"})
    with SqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
        agent = create_deep_agent(
            model=llm,
            tools=[fixture_lookup],
            checkpointer=checkpointer,
            system_prompt="Use the provided fixture_lookup tool when the task requests it. Keep final answers concise.",
        )
        config = {"configurable": {"thread_id": run_id}}
        last_messages: list[Any] = []
        update_count = 0
        for update in agent.stream({"messages": [{"role": "user", "content": prompt}]}, config, stream_mode="updates"):
            if not isinstance(update, dict):
                continue
            update_count += 1
            summaries: list[dict[str, Any]] = []
            for node, value in update.items():
                node_summary: dict[str, Any] = {"node": str(node)}
                if isinstance(value, dict) and isinstance(value.get("messages"), list):
                    last_messages = value["messages"]
                    node_summary["messages"] = [_message_summary(message) for message in value["messages"][-4:]]
                summaries.append(node_summary)
            emit("graph_update", {"updates": summaries})
        if not last_messages:
            state = agent.get_state(config)
            values = getattr(state, "values", {})
            if isinstance(values, dict) and isinstance(values.get("messages"), list):
                last_messages = values["messages"]
        answer = ""
        if last_messages:
            answer = str(getattr(last_messages[-1], "content", ""))
        emit("graph_completed", {"answer": answer[:2000], "update_count": update_count, "thread_id": run_id})
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True, help="JSON encoded request")
    args = parser.parse_args()
    try:
        return run(json.loads(args.request))
    except Exception as exc:  # pragma: no cover - exercised by subprocess smoke tests
        print(_json({"event_type": "worker_error", "payload": {"category": type(exc).__name__, "message": str(exc)[:1000]}}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
