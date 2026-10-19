"""External worker used by the Deep Agents + LangGraph adapter.

The worker is deliberately a small JSONL process.  Candidate dependencies are
installed in an external virtual environment, while this file remains in the
validation repository.  A request runs one graph invocation and emits only
structured event summaries; prompts, credentials and full model responses are
never written to the event stream by default.

Supported modes:

- ``run``: start a graph invocation.  If ``interrupt_on`` lists tool names, the
  LangGraph human-in-the-loop middleware pauses before those tools execute and
  emits ``interrupt_requested``.
- ``resume``: continue the same ``thread_id`` from its SQLite checkpoint with
  ``Command(resume=...)``.  This may run in a new process, which is the
  cross-process recovery path exercised by the adapter.
- ``inspect``: read the LangGraph SQLite checkpointer without invoking a model.
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


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return {"type": type(value).__name__, "repr": repr(value)[:500]}


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


def _state_summary(values: Any) -> dict[str, Any]:
    if not isinstance(values, dict):
        return {"type": type(values).__name__}
    summary: dict[str, Any] = {}
    for key, value in values.items():
        if key == "messages" and isinstance(value, list):
            summary[key] = {
                "count": len(value),
                "recent": [_message_summary(message) for message in value[-4:]],
            }
        elif value is None or isinstance(value, (str, int, float, bool)):
            summary[key] = value
        elif isinstance(value, (list, tuple)):
            summary[key] = {"type": type(value).__name__, "count": len(value)}
        else:
            summary[key] = {"type": type(value).__name__}
    return summary


def inspect_checkpoint(request: dict[str, Any]) -> int:
    """Read the latest checkpoint for a thread without invoking the model."""

    from langgraph.checkpoint.sqlite import SqliteSaver

    checkpoint_db = Path(str(request.get("checkpoint_db") or "langgraph-checkpoints.sqlite")).expanduser()
    thread_id = str(request.get("run_id") or "")
    if not thread_id:
        raise ValueError("inspect request requires run_id")
    if not checkpoint_db.exists():
        raise FileNotFoundError(f"checkpoint database does not exist: {checkpoint_db}")

    config = {"configurable": {"thread_id": thread_id}}
    with SqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
        latest = checkpointer.get_tuple(config)
        count = sum(1 for _ in checkpointer.list(config))
        if latest is None:
            print(_json({"event_type": "checkpoint_missing", "payload": {"thread_id": thread_id, "checkpoint_count": count}}), flush=True)
            return 2
        checkpoint_id = latest.config.get("configurable", {}).get("checkpoint_id")
        print(
            _json(
                {
                    "event_type": "checkpoint_inspected",
                    "payload": {
                        "thread_id": thread_id,
                        "checkpoint_id": checkpoint_id,
                        "checkpoint_count": count,
                        "metadata": latest.metadata,
                        "state": _state_summary(latest.checkpoint.get("channel_values")),
                        "pending_writes": len(latest.pending_writes or ()),
                    },
                }
            ),
            flush=True,
        )
    return 0


def _interrupt_payload(interrupts: Any) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for item in interrupts or ():
        payload.append(
            {
                "id": getattr(item, "id", None),
                "value": _jsonable(getattr(item, "value", None)),
                "resumable": getattr(item, "resumable", None),
                "ns": list(getattr(item, "ns", ()) or ()),
            }
        )
    return payload


def _stream_graph(agent: Any, payload: Any, config: dict[str, Any], emit: Any) -> tuple[list[dict[str, Any]], list[Any], int, bool]:
    """Stream graph updates, collecting interrupts instead of swallowing them.

    ``interrupt_after`` yields an empty ``__interrupt__`` tuple; the presence of
    the key is therefore the pause signal, not the payload length.
    """

    interrupts: list[dict[str, Any]] = []
    last_messages: list[Any] = []
    update_count = 0
    interrupt_seen = False
    for update in agent.stream(payload, config, stream_mode="updates"):
        if not isinstance(update, dict):
            continue
        if "__interrupt__" in update:
            interrupt_seen = True
            interrupts.extend(_interrupt_payload(update["__interrupt__"]))
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
    return interrupts, last_messages, update_count, interrupt_seen


def run(request: dict[str, Any]) -> int:
    # Imports are intentionally inside the worker entrypoint.  The parent
    # process does not need candidate packages installed in the repository venv.
    from deepagents import create_deep_agent
    from langchain_core.tools import tool
    from langchain_openai import ChatOpenAI
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import Command

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

    mode = str(request.get("mode") or "run")
    run_id = str(request.get("run_id") or "")
    if not run_id:
        raise ValueError("request requires run_id")
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

    interrupt_names = [str(name) for name in request.get("interrupt_on") or ()]
    interrupt_on = {name: True for name in interrupt_names} or None
    pause_after_model = bool(request.get("pause_after_model"))

    config: dict[str, Any] = {"configurable": {"thread_id": run_id}}
    checkpoint_id = request.get("checkpoint_id")
    if checkpoint_id:
        config["configurable"]["checkpoint_id"] = str(checkpoint_id)

    with SqliteSaver.from_conn_string(str(checkpoint_db)) as checkpointer:
        agent = create_deep_agent(
            model=llm,
            tools=[fixture_lookup],
            checkpointer=checkpointer,
            interrupt_on=interrupt_on,
            system_prompt="Use the provided fixture_lookup tool when the task requests it. Keep final answers concise.",
        )
        # ``interrupt_after=['model']`` is a non-approval safe point: the graph
        # stops after the current model turn and can resume in another process.
        graph = agent.builder.compile(checkpointer=checkpointer, interrupt_after=["model"]) if pause_after_model else agent
        pause_mode = pause_after_model and not interrupt_on

        if mode == "resume":
            resume_value = request.get("resume", True)
            emit("graph_resumed", {"thread_id": run_id, "checkpoint_id": checkpoint_id, "resume": resume_value})
            payload: Any = Command(resume=resume_value)
        else:
            prompt = request.get("prompt")
            if not isinstance(prompt, str) or not prompt.strip():
                raise ValueError("run request requires a non-empty prompt")
            emit(
                "graph_started",
                {
                    "thread_id": run_id,
                    "checkpoint_backend": "sqlite",
                    "interrupt_on": interrupt_names,
                    "pause_after_model": pause_after_model,
                },
            )
            payload = {"messages": [{"role": "user", "content": prompt}]}

        interrupts, last_messages, update_count, interrupt_seen = _stream_graph(graph, payload, config, emit)

        if interrupt_seen:
            if pause_mode:
                emit(
                    "pause_requested",
                    {"thread_id": run_id, "interrupts": interrupts, "update_count": update_count, "pause_kind": "after_model"},
                )
                emit("graph_paused", {"thread_id": run_id, "pause_kind": "after_model"})
            else:
                emit(
                    "interrupt_requested",
                    {"thread_id": run_id, "interrupts": interrupts, "update_count": update_count},
                )
                emit("graph_interrupted", {"thread_id": run_id, "interrupt_count": len(interrupts)})
            return 0

        if not last_messages:
            state = graph.get_state(config)
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
        request = json.loads(args.request)
        if request.get("mode") == "inspect":
            return inspect_checkpoint(request)
        return run(request)
    except Exception as exc:  # pragma: no cover - exercised by subprocess smoke tests
        print(_json({"event_type": "worker_error", "payload": {"category": type(exc).__name__, "message": str(exc)[:1000]}}), flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
