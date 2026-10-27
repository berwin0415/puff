"""在 OpenHands 原有工具面上增加查询回执工具与写入前数量规则。

只使用 OpenHands 公开的 ClientTool / ToolExecutor 接口；不重写 SDK 循环，
不代办业务步骤，也不为模型补写或补查。
"""

from copy import deepcopy
import json
import time

from openhands.sdk.tool import Tool, ToolExecutor, register_tool
from openhands.sdk.tool.client_tool import ClientTool, ClientToolObservation, ClientToolSpec

QUERY_TOOL = "query_order_status"


class QueryReceiptTool(ClientTool):
    """只把一次真实回执查询转发给共享工具服务。"""

    @property
    def name(self):
        return self.client_tool_name

    def _get_tool_schema(self, add_security_risk_prediction=False, action_type=None):
        if action_type is not None:
            raise ValueError("业务扩展必须使用原始工具结构")
        return deepcopy(self.input_schema)


class QueryReceiptExecutor(ToolExecutor):
    def __init__(self, client, deadline, evidence, scope, observations=None):
        self.client = client
        self.deadline = deadline
        self.evidence = evidence
        self.scope = scope
        self.observations = observations if observations is not None else []

    def __call__(self, action, conversation=None):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            return ClientToolObservation.from_text("分发工具前已达到活跃任务执行时限", is_error=True)
        arguments = action.model_dump(mode="json", exclude={"kind"})
        if set(arguments) - {"operation_id"}:
            return ClientToolObservation.from_text("回执查询参数不符合预期", is_error=True)
        call_id = f"query-{time.time_ns()}"
        record = {
            "time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "scope": self.scope,
            "call_id": call_id, "tool": "receipt", "exposed_tool": QUERY_TOOL,
            "arguments": arguments,
        }
        # 与公共审计一致：一次真实执行写 start/finish 配对。
        self.evidence.append("http-tool-events.jsonl", {**record, "phase": "start"})
        try:
            response = self.client.post("/call", json={"name": "receipt", "arguments": arguments}, timeout=remaining)
            payload = response.json()
        except Exception as error:
            self.evidence.append("http-tool-events.jsonl", {
                **record, "phase": "finish", "status": 0, "error": str(error)})
            return ClientToolObservation.from_text("回执查询传输失败；结果未知", is_error=True)
        source = self.evidence.append("http-tool-events.jsonl", {
            **record, "phase": "finish", "status": response.status_code, "result": payload})
        if response.status_code == 200:
            operation = arguments.get("operation_id")
            self.observations.append((call_id, operation, payload, source))
        return ClientToolObservation.from_text(
            json.dumps({"status": response.status_code, "body": payload}, ensure_ascii=False),
            is_error=response.is_error,
        )


class QuantityRuleExecutor(ToolExecutor):
    """在真正转发 HTTP 之前拦截超量写入。"""

    def __init__(self, inner, quantity_limit, evidence, scope):
        self.inner = inner
        self.quantity_limit = quantity_limit
        self.evidence = evidence
        self.scope = scope

    def __call__(self, action, conversation=None):
        arguments = action.model_dump(mode="json", exclude={"kind"})
        quantity = arguments.get("quantity")
        if type(quantity) is int and quantity > self.quantity_limit:
            call_id = f"quantity-{time.time_ns()}"
            record = {
                "time": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime()), "scope": self.scope,
                "call_id": call_id, "tool": "write",
                # guard：在真实转发之前由执行层规则拦下。
                "phase": "guard", "arguments": arguments,
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
                "operation_id": arguments.get("operation_id"),
                "quantity": quantity, "quantity_limit": self.quantity_limit,
                "source": source,
            })
            return ClientToolObservation.from_text(
                json.dumps({
                    "error": "业务写入数量超过执行规则的限制",
                    "reason": "quantity_limit_exceeded",
                    "approval_required": False,
                    "quantity": quantity,
                    "quantity_limit": self.quantity_limit,
                }, ensure_ascii=False),
                is_error=True,
            )
        return self.inner(action, conversation)


def install_query_tool(client, deadline, evidence, scope, receipt_schema, observations=None):
    """按共享服务的原始 receipt 结构注册查询工具。"""
    specification = ClientToolSpec(
        name=QUERY_TOOL,
        description="查询指定操作编号的真实回执与实际写入次数。只发起一次查询，不代办后续步骤。",
        parameters=deepcopy(receipt_schema),
    )
    tool = QueryReceiptTool.from_spec(specification).model_copy(update={
        "executor": QueryReceiptExecutor(client, deadline, evidence, scope, observations),
    })
    register_tool(QUERY_TOOL, tool)
    return Tool(name=QUERY_TOOL)


def aggregate_business_state(scope, observations):
    """从真实回执查询观察结果聚合业务状态。

    observations 为 (call_id, operation_id, 结果体, 原生事件文件相对路径, 行号) 序列。
    只统计 receipt 工具在 200 下返回、且 operation/scope/write_count/receipts 自洽的条目；
    写入应答本身不被当作业务确认。
    """
    operations = {}
    last = None
    for call_id, operation, body, source in observations:
        result = body.get("result") if isinstance(body, dict) else None
        if not isinstance(result, dict) or result.get("operation_id") != operation:
            continue
        receipts, count = result.get("receipts"), result.get("write_count")
        if (type(count) is not int or count < 1 or not isinstance(receipts, list) or len(receipts) != count
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
        "schema_version": 1, "scope": deepcopy(scope),
        "operation_id": last, "receipt_confirmed": bool(last),
        "operations": operations,
    }
