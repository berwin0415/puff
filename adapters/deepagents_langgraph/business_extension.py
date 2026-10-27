"""通过公开 LangGraph 状态通道保存业务观测，不另建运行器。

归并器合并已完成的观测，避免并发工具调用互相覆盖状态。
只有实际工具响应才能确认回执。此处不查询回执、推进任务或重放用户提示词。
"""

from copy import deepcopy
import time


def new_business_state(scope):
    return {"schema_version": 1, "scope": deepcopy(scope), "operation_id": None,
        "receipt_confirmed": False, "operations": {}, "observations": {}}


def quantity_denial(arguments, quantity_limit):
    quantity = arguments.get("quantity")
    if type(quantity) is int and quantity > quantity_limit:
        return {"error": "业务写入数量超过执行规则的限制", "reason": "quantity_limit_exceeded",
            "approval_required": False, "quantity": quantity, "quantity_limit": quantity_limit,
            "operation_id": arguments.get("operation_id"), "http_forwarded": False}
    return None


def observation_delta(scope, call_id, tool, arguments, status, payload, source):
    state = new_business_state(scope)
    operation = arguments.get("operation_id")
    if tool not in {"write", "receipt"} or not isinstance(operation, str) or not 0 < len(operation) <= 128:
        return state
    result = payload.get("result") if isinstance(payload, dict) else None

    def matching(receipt):
        return (isinstance(receipt, dict) and receipt.get("operation_id") == operation
            and all(receipt.get(key) == value for key, value in scope.items()))

    observation = {"operation_id": operation, "tool": tool, "observed_at_ns": time.time_ns(),
        "source": deepcopy(source), "receipt_confirmed": False, "write_acknowledged": False,
        "write_count": None, "receipts": [],
        "last_observation": "write_result_unknown" if tool == "write" else "receipt_query_not_confirmed"}
    if tool == "write" and status == 403 and isinstance(payload, dict) and payload.get("reason") == "quantity_limit_exceeded":
        observation["last_observation"] = "write_rejected_before_http"
    if status == 200 and isinstance(result, dict):
        if tool == "write" and matching(result.get("receipt")):
            observation.update({"write_acknowledged": True, "last_observation": "write_response_received_receipt_query_pending"})
        if tool == "receipt":
            receipts, count = result.get("receipts"), result.get("write_count")
            if (result.get("operation_id") == operation and type(count) is int and count > 0
                    and isinstance(receipts, list) and len(receipts) == count and all(matching(receipt) for receipt in receipts)):
                observation.update({"receipt_confirmed": True, "write_count": count, "receipts": deepcopy(receipts),
                    "last_observation": "actual_receipt_query_confirmed"})
    state["observations"][call_id] = observation
    return state


def merge_business_state(left, right):
    """公开状态通道归并器；完整工具观测不可变。"""
    if not left:
        left = new_business_state(right["scope"])
    if not right:
        return deepcopy(left)
    if left.get("schema_version") != 1 or right.get("schema_version") != 1 or left.get("scope") != right.get("scope"):
        raise ValueError("业务状态的作用域或结构不匹配")
    state = new_business_state(left["scope"])
    observations = {**deepcopy(left["observations"]), **deepcopy(right["observations"])}
    for call_id in left["observations"].keys() & right["observations"].keys():
        if left["observations"][call_id] != right["observations"][call_id]:
            raise ValueError("同一次原生工具调用出现相互冲突的业务观测")
    state["observations"] = observations
    for call_id, observed in sorted(observations.items(), key=lambda item: (item[1]["observed_at_ns"], item[0])):
        operation = observed["operation_id"]
        entry = state["operations"].setdefault(operation, {"operation_id": operation, "receipt_confirmed": False,
            "write_count": None, "receipts": [], "write_acknowledged": False, "last_observation": "not_observed", "source": None})
        if observed["tool"] == "write":
            entry["receipt_confirmed"] = False
        entry.update({"last_observation": observed["last_observation"], "source": deepcopy(observed["source"]), "native_tool_call_id": call_id})
        entry["write_acknowledged"] |= observed["write_acknowledged"]
        if observed["receipt_confirmed"]:
            entry.update({key: deepcopy(observed[key]) for key in ("receipt_confirmed", "write_count", "receipts")})
        state["operation_id"], state["receipt_confirmed"] = operation, entry["receipt_confirmed"]
    return state


def read_business_state(values, expected_scope):
    state = values.get("business_state") if isinstance(values, dict) else None
    if (not isinstance(state, dict) or state.get("schema_version") != 1 or state.get("scope") != expected_scope
            or not isinstance(state.get("operations"), dict) or not isinstance(state.get("observations"), dict)
            or type(state.get("receipt_confirmed")) is not bool):
        raise ValueError("原生图状态缺少可信业务通道")
    rebuilt = merge_business_state(new_business_state(expected_scope), state)
    if rebuilt != state:
        raise ValueError("原生业务状态投影与观测记录不一致")
    return deepcopy(state)


def state_schema():
    """仅在候选内部构建文档所述的自定义 DeepAgentState。"""
    from typing import Annotated
    from deepagents import DeepAgentState

    class BusinessDeepAgentState(DeepAgentState):
        business_state: Annotated[dict, merge_business_state]

    return BusinessDeepAgentState
