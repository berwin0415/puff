"""暴露可信 HTTP 工具结构，不选择任何任务步骤。"""

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
        # 仅由单独构建的 I23 镜像或请求显式启用；普通
        # I22 路径没有此绑定、额外 HTTP 请求或任务决策变化。
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
            # 仅发出一次原始请求，不重试，也不自动查询回执。
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
                return None, None, source, "实验写入前门控已拒绝；此桥接器未发出业务 HTTP 请求"
            except httpx.HTTPError as error:
                source = self.event({"call_id": call_id, "tool": canonical_name, "exposed_tool": exposed_name, "phase": "error",
                    "arguments": arguments, "error_type": type(error).__name__, "error": str(error)})
                return None, None, source, "HTTP 工具传输失败；副作用状态未知"
            try:
                body = result.json()
            except ValueError:
                source = self.event({"call_id": call_id, "tool": canonical_name, "exposed_tool": exposed_name, "phase": "error",
                    "arguments": arguments, "status": result.status_code, "error": "invalid_json_response"})
                return result.status_code, None, source, f"HTTP 工具返回了无效 JSON，状态码={result.status_code}"
            source = self.event({"call_id": call_id, "tool": canonical_name, "exposed_tool": exposed_name, "phase": "finish",
                "status": result.status_code, "arguments": arguments, "result": body})
            return result.status_code, body, source, None

        if not self.extension:
            from langgraph.prebuilt import ToolRuntime

            async def execute(runtime: ToolRuntime, **arguments: Any) -> str:
                native_call_id = runtime.tool_call_id
                if not isinstance(native_call_id, str) or not native_call_id:
                    raise ValueError("必须提供实际注入的原生工具调用 ID")
                status, body, _, error = await request(arguments, uuid4().hex, native_call_id=native_call_id)
                if error:
                    raise ToolException(error)
                text = json.dumps(body, ensure_ascii=False)
                if status >= 400:
                    raise ToolException(f"HTTP 工具状态码={status}: {text}")
                return text
            return execute

        from langchain_core.messages import ToolMessage
        from langgraph.prebuilt import ToolRuntime
        from langgraph.types import Command

        async def execute(runtime: ToolRuntime, **arguments: Any):
            # 公开 ToolNode 在模型 JSON 结构之外注入 runtime。
            # 不得直接修改 runtime.state：只有 Command 更新会进入检查点。
            read_business_state(runtime.state, self.scope)
            call_id = runtime.tool_call_id
            if not isinstance(call_id, str) or not call_id:
                raise ValueError("必须提供真实的原生工具调用 ID")
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
            raise ValueError("可信工具清单无效")
        tools, schemas = [], {}
        for entry in payload["tools"]:
            name = entry.get("name")
            if not isinstance(name, str) or not name or not name.replace("_", "").isalnum() or name in self.names.values():
                raise ValueError("可信工具名称无效或重复")
            description, schema = entry.get("description"), entry.get("input_schema")
            if not isinstance(description, str) or not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("可信工具结构无效")
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
            raise ValueError("可信工具清单为空")
        if self.extension:
            if "receipt" not in schemas:
                raise ValueError("业务查询必须使用实际回执工具结构")
            self.names["query_order_status"] = "receipt"
            tools.append(StructuredTool(name="query_order_status",
                description="查询当前用户和会话中某个 operation_id 的实际回执及 write_count。此工具只读，不执行写入或剩余任务步骤。",
                args_schema=schemas["receipt"], coroutine=self.forward("receipt", "query_order_status"), handle_tool_error=True))
        return tools

    async def close(self) -> None:
        await self.client.aclose()
