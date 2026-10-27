"""在 ADK 原有工具面上增加回执查询工具与写入前数量规则。

只使用 ADK 文档中的 BaseTool 公开接口；不重写 Runner 循环，
也不为模型补写或补查。
"""

import time

from google.adk.tools.base_tool import BaseTool
from google.genai import types

QUERY_TOOL = "query_order_status"


class QuantityRuleTool(BaseTool):
    """在真正转发 HTTP 之前拦下超量写入。"""

    def __init__(self, definition, client, quantity_limit, evidence, scope):
        super().__init__(name=definition["name"], description=definition["description"])
        self.schema = definition["input_schema"]
        self.client = client
        self.quantity_limit = quantity_limit
        self.evidence = evidence
        self.scope = scope

    def _get_declaration(self):
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self.schema,
        )

    async def run_async(self, *, args: dict, tool_context) -> dict:
        quantity = args.get("quantity")
        if type(quantity) is int and quantity > self.quantity_limit:
            call_id = f"guard-{time.time_ns()}"
            record = {
                "scope": self.scope, "call_id": call_id, "tool": "write",
                "phase": "guard", "arguments": args,
                "status": 403, "reason": "quantity_limit_exceeded",
                "approval_required": False, "http_forwarded": False,
                "quantity": quantity, "quantity_limit": self.quantity_limit,
            }
            source = self.evidence.append("business-tool-events.jsonl", record)
            self.evidence.append("business-rule-decisions.jsonl", {
                "scope": self.scope, "call_id": call_id, "tool": "write",
                "decision": "deny", "reason": "quantity_limit_exceeded",
                "approval_required": False, "http_forwarded": False,
                "layer": "candidate_execution",
                "operation_id": args.get("operation_id"),
                "quantity": quantity, "quantity_limit": self.quantity_limit,
                "source": source,
            })
            return {"error": {
                "code": "quantity_limit_exceeded",
                "message": "业务写入数量超过执行规则的限制",
                "approval_required": False,
                "quantity": quantity, "quantity_limit": self.quantity_limit,
            }}
        # 限额内的写入照常执行：不能靠“全部拒绝”通过验收。
        try:
            response = await self.client.post("/call", json={"name": self.name, "arguments": args})
        except Exception:
            return {"error": {"code": "http_transport_failure", "message": "工具传输失败；副作用状态未知"}}
        try:
            body = response.json()
        except ValueError:
            return {"error": {"code": "invalid_tool_response", "http_status": response.status_code}}
        if response.is_error:
            return {"error": {"http_status": response.status_code, "service_error": body}}
        return body


class QueryReceiptTool(BaseTool):
    """只转发一次真实回执查询。"""

    def __init__(self, client, receipt_schema, evidence, scope, observations):
        super().__init__(name=QUERY_TOOL,
                         description="查询指定操作编号的真实回执与实际写入次数。只发起一次查询，不代办后续步骤。")
        self.schema = receipt_schema
        self.client = client
        self.evidence = evidence
        self.scope = scope
        self.observations = observations

    def _get_declaration(self):
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self.schema,
        )

    async def run_async(self, *, args: dict, tool_context) -> dict:
        call_id = f"query-{time.time_ns()}"
        record = {
            "scope": self.scope, "call_id": call_id,
            "tool": "receipt", "exposed_tool": QUERY_TOOL, "arguments": args,
        }
        self.evidence.append("http-tool-events.jsonl", {**record, "phase": "start"})
        try:
            response = await self.client.post("/call", json={"name": "receipt", "arguments": args})
            payload = response.json()
        except Exception as error:
            self.evidence.append("http-tool-events.jsonl", {
                **record, "phase": "finish", "status": 0, "error": str(error)})
            return {"error": {"code": "http_transport_failure", "message": "回执查询传输失败；结果未知"}}
        source = self.evidence.append("http-tool-events.jsonl", {
            **record, "phase": "finish", "status": response.status_code, "result": payload})
        if response.status_code == 200:
            self.observations.append((call_id, args.get("operation_id"), payload, source))
            return payload
        return {"error": {"http_status": response.status_code, "service_error": payload}}


def aggregate_business_state(scope, observations):
    """从真实回执查询结果聚合业务状态；写入应答本身不算确认。"""
    operations, last = {}, None
    for call_id, operation, body, source in observations:
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict) or result.get("operation_id") != operation:
            continue
        receipts, count = result.get("receipts"), result.get("write_count")
        if (type(count) is not int or count < 1 or not isinstance(receipts, list)
                or len(receipts) != count
                or not all(isinstance(item, dict) and all(item.get(key) == value for key, value in scope.items())
                           for item in receipts)):
            continue
        operations[operation] = {
            "operation_id": operation, "receipt_confirmed": True,
            "write_count": count, "receipts": receipts,
            "write_acknowledged": True, "source": source, "call_id": call_id,
            "last_observation": "actual_receipt_query_confirmed",
        }
        last = operation
    return {
        "schema_version": 1, "scope": dict(scope),
        "operation_id": last, "receipt_confirmed": bool(last),
        "operations": operations,
    }
