"""Expose the trusted HTTP tool schemas without selecting any task steps."""

import json
from typing import Any
from uuid import uuid4

import httpx
from langchain_core.tools import StructuredTool, ToolException

from business_extension import observation_delta, quantity_denial, read_business_state
from detailed_support import denial_from_http
from prewrite_gate import PrewriteClient, PrewriteDenied


class HttpTools:
    def __init__(self, url: str, token: str, timeout_seconds: int, on_event=None, *, evidence=None, extension=None, scope=None):
        # Opt-in only for a separately built I23 image/request. The ordinary
        # I22 path has no binding, no extra HTTP and no changed task decisions.
        self.prewrite = PrewriteClient.from_environment(url, scope, evidence)
        self.client = httpx.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout_seconds,
            trust_env=False,
            transport=httpx.AsyncHTTPTransport(retries=0),
        )
        self.names: dict[str, str] = {}
        self.on_event = on_event
        self.denied = False
        self.evidence, self.extension, self.scope = evidence, extension, scope
        self.quantity_denied = False

    def event(self, value):
        if value.get("phase") == "finish" and denial_from_http(value["tool"], value["status"], value.get("result")):
            self.denied = True
        if self.on_event is not None:
            observed = self.on_event(value)
            self.denied |= observed is True
            return observed if isinstance(observed, dict) else None
        return None

    def forward(self, canonical_name, exposed_name):
        async def request(arguments, call_id, *, native_call_id=None):
            # One original request: no retry or automatic receipt query.
            self.event({"call_id": call_id, "tool": canonical_name, "exposed_tool": exposed_name, "phase": "start", "arguments": arguments})
            try:
                headers = await self.prewrite.before(self.client, canonical_name, arguments, call_id,
                    native_call_id=native_call_id) if self.prewrite else None
                result = await self.client.post("/call", json={"name": canonical_name, "arguments": arguments},
                    **({"headers": headers} if headers is not None else {}))
            except PrewriteDenied:
                source = self.event({"call_id": call_id, "tool": canonical_name, "exposed_tool": exposed_name,
                    "phase": "error", "arguments": arguments, "http_forwarded": False,
                    "error_type": "PrewriteDenied", "side_effect_state": "not_dispatched_by_this_bridge"})
                return None, None, source, "Experiment prewrite gate refused; this bridge did not send the business HTTP request"
            except httpx.HTTPError as error:
                source = self.event({"call_id": call_id, "tool": canonical_name, "exposed_tool": exposed_name, "phase": "error",
                    "arguments": arguments, "error_type": type(error).__name__, "error": str(error)})
                return None, None, source, "HTTP tool transport failure; side-effect state is unknown"
            try:
                body = result.json()
            except ValueError:
                source = self.event({"call_id": call_id, "tool": canonical_name, "exposed_tool": exposed_name, "phase": "error",
                    "arguments": arguments, "status": result.status_code, "error": "invalid_json_response"})
                return result.status_code, None, source, f"HTTP tool returned invalid JSON, status={result.status_code}"
            source = self.event({"call_id": call_id, "tool": canonical_name, "exposed_tool": exposed_name, "phase": "finish",
                "status": result.status_code, "arguments": arguments, "result": body})
            return result.status_code, body, source, None

        if not self.extension:
            from langgraph.prebuilt import ToolRuntime

            async def execute(runtime: ToolRuntime, **arguments: Any) -> str:
                native_call_id = runtime.tool_call_id
                if not isinstance(native_call_id, str) or not native_call_id:
                    raise ValueError("Expected the actual injected native tool call ID")
                status, body, _, error = await request(arguments, uuid4().hex, native_call_id=native_call_id)
                if error:
                    raise ToolException(error)
                text = json.dumps(body, ensure_ascii=False)
                if status >= 400:
                    raise ToolException(f"HTTP tool status={status}: {text}")
                return text
            return execute

        from langchain_core.messages import ToolMessage
        from langgraph.prebuilt import ToolRuntime
        from langgraph.types import Command

        async def execute(runtime: ToolRuntime, **arguments: Any):
            # The public ToolNode injects runtime outside the model JSON schema.
            # Never mutate runtime.state: only Command updates are checkpointed.
            read_business_state(runtime.state, self.scope)
            call_id = runtime.tool_call_id
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("Expected an actual native tool call ID")
            denial = quantity_denial(arguments, self.extension["quantity_limit"]) if canonical_name == "write" else None
            if denial:
                self.quantity_denied = True
                base = {"scope": self.scope, "call_id": call_id, "tool": canonical_name,
                    "exposed_tool": exposed_name, "arguments": arguments}
                source = self.evidence.append("business-tool-events.jsonl", {**base, "time": self.evidence.now(),
                    "phase": "finish", "status": 403, "http_forwarded": False, "result": denial})
                self.evidence.append("business-rule-decisions.jsonl", {"scope": self.scope, "time": self.evidence.now(),
                    "call_id": call_id, "tool": canonical_name, "operation_id": arguments.get("operation_id"),
                    "decision": "deny", "reason": denial["reason"], "approval_required": False,
                    "layer": "candidate_execution", "status": 403, "quantity": arguments["quantity"],
                    "quantity_limit": self.extension["quantity_limit"], "http_forwarded": False, "source": source})
                status, body, error = 403, denial, None
            else:
                status, body, source, error = await request(arguments, call_id, native_call_id=call_id)
            content = ({"ok": False, "status": status, "error": error} if error else {"status": status, "body": body})
            message = ToolMessage(content=json.dumps(content, ensure_ascii=False), tool_call_id=call_id,
                name=exposed_name, status="error" if error or status is None or status >= 400 else "success")
            return Command(update={"business_state": observation_delta(self.scope, call_id, canonical_name,
                arguments, status, body, source), "messages": [message]})

        return execute

    async def load(self) -> list[StructuredTool]:
        response = await self.client.get("/tools")
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list):
            raise ValueError("Invalid trusted tools listing")
        tools, schemas = [], {}
        for entry in payload["tools"]:
            name = entry.get("name")
            if not isinstance(name, str) or not name or not name.replace("_", "").isalnum() or name in self.names.values():
                raise ValueError("Invalid or duplicate trusted tool name")
            description, schema = entry.get("description"), entry.get("input_schema")
            if not isinstance(description, str) or not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("Invalid trusted tool schema")
            display_name = "lab_" + name
            self.names[display_name] = name
            schemas[name] = schema
            tools.append(StructuredTool(
                name=display_name,
                description=description,
                args_schema=schema,
                coroutine=self.forward(name, display_name),
                handle_tool_error=True,
            ))
        if not tools:
            raise ValueError("Trusted tool listing is empty")
        if self.extension:
            if "receipt" not in schemas:
                raise ValueError("The business query requires the actual receipt tool schema")
            self.names["query_order_status"] = "receipt"
            tools.append(StructuredTool(name="query_order_status",
                description="Query actual receipts and write_count for one operation_id in the current user/session. This tool only reads; it performs no writes or remaining task steps.",
                args_schema=schemas["receipt"], coroutine=self.forward("receipt", "query_order_status"), handle_tool_error=True))
        return tools

    async def close(self) -> None:
        await self.client.aclose()
