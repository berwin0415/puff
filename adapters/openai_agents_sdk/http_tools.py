"""Bind original HTTP schemas and an optional read-only business query tool."""

import json
from uuid import uuid4

from agents import FunctionTool
import httpx2

from business_extension import begin_operation, observe_operation, quantity_denial
from detailed_support import denial_from_http
from tool_definitions import save_authenticated_definitions, validate_definitions


class HttpTools:
    def __init__(self, url: str, token: str, timeout_seconds: int, identity: dict, evidence=None, extension=None, *, offline=False):
        self.identity = identity
        self.expected_identity = {key: value for key, value in identity.items() if key != "business_state"}
        self.evidence, self.denied = evidence, False
        self.extension, self.quantity_denied = extension, False
        self.offline = offline
        self.client = None if offline else httpx2.AsyncClient(
            base_url=url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=min(timeout_seconds, 30),
            trust_env=False,
            follow_redirects=False,
            transport=httpx2.AsyncHTTPTransport(retries=0),
        )

    async def load(self, *, state_dir=None, provenance=None) -> list[FunctionTool]:
        if self.offline:
            raise ValueError("Offline reconstruction cannot discover tools over HTTP")
        response = await self.client.get("/tools")
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("Expected HTTP 200 for trusted tool discovery")
        definitions = validate_definitions(response.json())
        if self.extension:
            binding = save_authenticated_definitions(state_dir, response.content, self.identity, provenance)
            self.identity["tool_definition_cache"] = binding
            self.expected_identity = {key: value for key, value in self.identity.items() if key != "business_state"}
        return self.build(definitions)

    def build(self, definitions) -> list[FunctionTool]:
        """Build identical public FunctionTools from authenticated live/cache data."""
        validate_definitions({"tools": definitions})
        tools, schemas = [], {}
        for definition in definitions:
            name, description, schema = definition.get("name"), definition.get("description"), definition.get("input_schema")
            if not isinstance(description, str) or not isinstance(schema, dict) or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise ValueError("Invalid original tool schema")
            schemas[name] = schema
            tools.append(FunctionTool(name=name, description=description, params_json_schema=schema,
                on_invoke_tool=self._forward(name, name), strict_json_schema=False, needs_approval=False))
        if self.extension:
            tools.append(FunctionTool(name="query_order_status",
                description="Query actual receipts and write_count for one operation_id in the current user/session. This tool only reads; it performs no writes or remaining task steps.",
                params_json_schema=schemas["receipt"], on_invoke_tool=self._forward("receipt", "query_order_status"),
                strict_json_schema=False, needs_approval=False))
        return tools

    def _active_context(self, context):
        active = context.context
        if not self.extension:
            if active != self.identity:
                raise ValueError("Tool invocation has the wrong trusted run identity")
        elif (not isinstance(active, dict) or set(active) != set(self.expected_identity) | {"business_state"}
                or any(active.get(key) != value for key, value in self.expected_identity.items())
                or not isinstance(active.get("business_state"), dict)
                or not isinstance(active["business_state"].get("operations"), dict)):
            raise ValueError("Restored business tool context has the wrong trusted run identity")
        return active

    def _observe(self, active, name, arguments, status, payload, source):
        if self.extension:
            observe_operation(active, name, arguments, status, payload, source)

    def _forward(self, canonical_name, exposed_name):
        async def invoke(context, raw_arguments):
            if self.offline:
                raise ValueError("Offline state reconstruction cannot invoke a tool")
            active = self._active_context(context)
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Expected object tool arguments")
            call_id = getattr(context, "tool_call_id", None) or uuid4().hex
            base = {"scope": self.evidence.scope if self.evidence else None, "call_id": call_id,
                    "tool": canonical_name, "exposed_tool": exposed_name, "arguments": arguments}
            if self.extension:
                begin_operation(active, canonical_name, arguments)
                denial = quantity_denial(arguments, self.extension["quantity_limit"]) if canonical_name == "write" else None
                if denial:
                    self.quantity_denied = True
                    source = self.evidence.append("business-tool-events.jsonl", {**base, "time": self.evidence.now(),
                        "phase": "finish", "status": 403, "http_forwarded": False, "result": denial})
                    self.evidence.append("business-rule-decisions.jsonl", {"scope": self.evidence.scope,
                        "time": self.evidence.now(), "call_id": call_id, "tool": canonical_name,
                        "operation_id": arguments.get("operation_id"), "decision": "deny", "reason": denial["reason"],
                        "approval_required": False, "layer": "candidate_execution", "status": 403,
                        "quantity": arguments["quantity"], "quantity_limit": self.extension["quantity_limit"],
                        "http_forwarded": False, "source": source})
                    self._observe(active, canonical_name, arguments, 403, denial, source)
                    return json.dumps({"status": 403, "body": denial}, ensure_ascii=False)
            if self.evidence:
                self.evidence.append("http-tool-events.jsonl", {**base, "time": self.evidence.now(), "phase": "start"})
            # One original request, no application retry or automatic receipt query.
            # Trusted HTTP service validates original schema and binds actual scope.
            try:
                result = await self.client.post("/call", json={"name": canonical_name, "arguments": arguments})
            except httpx2.HTTPError as error:
                source = self.evidence.append("http-tool-events.jsonl", {**base, "time": self.evidence.now(),
                    "phase": "error", "error_type": type(error).__name__, "error": str(error)}) if self.evidence else None
                self._observe(active, canonical_name, arguments, None, None, source)
                return json.dumps({"ok": False, "error": "HTTP tool transport failure; side-effect state is unknown"})
            try:
                payload = result.json()
            except ValueError:
                source = self.evidence.append("http-tool-events.jsonl", {**base, "time": self.evidence.now(),
                    "phase": "finish", "status": result.status_code, "body_text": result.text,
                    "parse_error": "invalid_json"}) if self.evidence else None
                self._observe(active, canonical_name, arguments, result.status_code, None, source)
                return json.dumps({"ok": False, "status": result.status_code, "error": "HTTP tool returned invalid JSON; side-effect state is unknown"})
            source = self.evidence.append("http-tool-events.jsonl", {**base, "time": self.evidence.now(),
                "phase": "finish", "status": result.status_code, "result": payload}) if self.evidence else None
            self._observe(active, canonical_name, arguments, result.status_code, payload, source)
            if self.evidence and denial_from_http(canonical_name, result.status_code, payload):
                self.denied = True
                self.evidence.append("policy-decisions.jsonl", {"scope": self.evidence.scope,
                    "time": self.evidence.now(), "call_id": call_id, "tool": canonical_name,
                    "operation_id": arguments.get("operation_id"), "decision": "deny",
                    "reason": "sensitive_operation_denied", "approval_required": False,
                    "layer": "tool_execution", "status": result.status_code,
                    "fixture_status": payload["fixture_status"], "source": source})
            return json.dumps({"status": result.status_code, "body": payload}, ensure_ascii=False)

        return invoke

    async def close(self) -> None:
        if self.client is not None:
            await self.client.aclose()
