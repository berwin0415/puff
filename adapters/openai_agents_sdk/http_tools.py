"""绑定原始 HTTP 结构和可选的只读业务查询工具。"""

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
            raise ValueError("离线重建不能通过 HTTP 发现工具")
        response = await self.client.get("/tools")
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("可信工具发现接口应返回 HTTP 200")
        definitions = validate_definitions(response.json())
        if self.extension:
            binding = save_authenticated_definitions(state_dir, response.content, self.identity, provenance)
            self.identity["tool_definition_cache"] = binding
            self.expected_identity = {key: value for key, value in self.identity.items() if key != "business_state"}
        return self.build(definitions)

    def build(self, definitions) -> list[FunctionTool]:
        """根据已认证的实时数据或缓存构建相同的公开 FunctionTools。"""
        validate_definitions({"tools": definitions})
        tools, schemas = [], {}
        for definition in definitions:
            name, description, schema = definition.get("name"), definition.get("description"), definition.get("input_schema")
            if not isinstance(description, str) or not isinstance(schema, dict) or schema.get("type") != "object" or schema.get("additionalProperties") is not False:
                raise ValueError("原始工具结构无效")
            schemas[name] = schema
            tools.append(FunctionTool(name=name, description=description, params_json_schema=schema,
                on_invoke_tool=self._forward(name, name), strict_json_schema=False, needs_approval=False))
        if self.extension:
            tools.append(FunctionTool(name="query_order_status",
                description="查询当前用户和会话中某个 operation_id 的实际回执及 write_count。此工具只读，不执行写入或剩余任务步骤。",
                params_json_schema=schemas["receipt"], on_invoke_tool=self._forward("receipt", "query_order_status"),
                strict_json_schema=False, needs_approval=False))
        return tools

    def _active_context(self, context):
        active = context.context
        if not self.extension:
            if active != self.identity:
                raise ValueError("工具调用的可信运行身份不匹配")
        elif (not isinstance(active, dict) or set(active) != set(self.expected_identity) | {"business_state"}
                or any(active.get(key) != value for key, value in self.expected_identity.items())
                or not isinstance(active.get("business_state"), dict)
                or not isinstance(active["business_state"].get("operations"), dict)):
            raise ValueError("恢复后的业务工具上下文的可信运行身份不匹配")
        return active

    def _observe(self, active, name, arguments, status, payload, source):
        if self.extension:
            observe_operation(active, name, arguments, status, payload, source)

    def _forward(self, canonical_name, exposed_name):
        async def invoke(context, raw_arguments):
            if self.offline:
                raise ValueError("离线状态重建不能调用工具")
            active = self._active_context(context)
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("工具参数必须是对象")
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
            # 仅发出一次原始请求，应用层不重试，也不自动查询回执。
            # 可信 HTTP 服务校验原始结构并绑定实际作用域。
            try:
                result = await self.client.post("/call", json={"name": canonical_name, "arguments": arguments})
            except httpx2.HTTPError as error:
                source = self.evidence.append("http-tool-events.jsonl", {**base, "time": self.evidence.now(),
                    "phase": "error", "error_type": type(error).__name__, "error": str(error)}) if self.evidence else None
                self._observe(active, canonical_name, arguments, None, None, source)
                return json.dumps({"ok": False, "error": "HTTP 工具传输失败；副作用状态未知"})
            try:
                payload = result.json()
            except ValueError:
                source = self.evidence.append("http-tool-events.jsonl", {**base, "time": self.evidence.now(),
                    "phase": "finish", "status": result.status_code, "body_text": result.text,
                    "parse_error": "invalid_json"}) if self.evidence else None
                self._observe(active, canonical_name, arguments, result.status_code, None, source)
                return json.dumps({"ok": False, "status": result.status_code, "error": "HTTP 工具返回了无效 JSON；副作用状态未知"})
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
