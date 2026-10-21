"""Application business fields inside the public SDK mapping run context.

No scheduler, conversation replay, automatic receipt lookup or substitute
checkpoint. The public Runner/RunState own persistence of this context.
"""
from copy import deepcopy


def new_business_state():
    return {"schema_version": 1, "operation_id": None, "receipt_confirmed": False, "operations": {}}


def quantity_denial(arguments, quantity_limit):
    quantity = arguments.get("quantity")
    if type(quantity) is int and quantity > quantity_limit:
        return {"error": "Business write quantity exceeds the execution rule", "reason": "quantity_limit_exceeded",
            "approval_required": False, "quantity": quantity, "quantity_limit": quantity_limit,
            "operation_id": arguments.get("operation_id"), "http_forwarded": False}
    return None


def begin_operation(context, tool, arguments):
    if tool not in {"write", "receipt"}:
        return
    operation = arguments.get("operation_id")
    if not isinstance(operation, str) or not 0 < len(operation) <= 128:
        return
    state = context["business_state"]
    entry = state["operations"].setdefault(operation, {"operation_id": operation,
        "receipt_confirmed": False, "write_count": None, "receipts": [],
        "write_acknowledged": False, "last_observation": "not_observed", "source": None})
    if tool == "write":
        entry["receipt_confirmed"] = False
        entry["last_observation"] = "write_in_flight"
    state["operation_id"], state["receipt_confirmed"] = operation, entry["receipt_confirmed"]


def observe_operation(context, tool, arguments, status, payload, source):
    operation = arguments.get("operation_id")
    state = context["business_state"]
    if tool not in {"write", "receipt"} or not isinstance(operation, str) or operation not in state["operations"]:
        return
    entry = state["operations"][operation]
    result = payload.get("result") if isinstance(payload, dict) else None
    entry["source"] = deepcopy(source)
    entry["last_observation"] = "write_result_unknown" if tool == "write" else "receipt_query_not_confirmed"
    if tool == "write" and status == 403 and isinstance(payload, dict) and payload.get("reason") == "quantity_limit_exceeded":
        entry["last_observation"] = "write_rejected_before_http"
    scope = context["scope"]

    def matching_receipt(receipt):
        return (isinstance(receipt, dict) and receipt.get("operation_id") == operation
                and all(receipt.get(key) == value for key, value in scope.items()))

    if status == 200 and isinstance(result, dict):
        if tool == "write" and matching_receipt(result.get("receipt")):
            entry["write_acknowledged"] = True
            entry["last_observation"] = "write_response_received_receipt_query_pending"
        if tool == "receipt":
            receipts, count = result.get("receipts"), result.get("write_count")
            if (result.get("operation_id") == operation and type(count) is int and count > 0
                    and isinstance(receipts, list) and len(receipts) == count
                    and all(matching_receipt(receipt) for receipt in receipts)):
                entry.update({"receipt_confirmed": True, "write_count": count,
                    "receipts": deepcopy(receipts), "last_observation": "actual_receipt_query_confirmed"})
    if state["operation_id"] == operation:
        state["receipt_confirmed"] = entry["receipt_confirmed"]


def read_business_state(native_json, expected_scope):
    """Read the documented JSON output, never SDK private object attributes."""
    envelope = native_json.get("context") if isinstance(native_json, dict) else None
    context = envelope.get("context") if isinstance(envelope, dict) else None
    if not isinstance(context, dict) or context.get("scope") != expected_scope:
        raise ValueError("Native RunState context has a different trusted scope")
    state = context.get("business_state")
    if (not isinstance(state, dict) or state.get("schema_version") != 1
            or not isinstance(state.get("operations"), dict)
            or type(state.get("receipt_confirmed")) is not bool):
        raise ValueError("Native RunState does not contain the business mapping context")
    operation = state.get("operation_id")
    if operation is not None and (not isinstance(operation, str) or operation not in state["operations"]):
        raise ValueError("Native business current operation is inconsistent")
    return deepcopy(state)


async def restore_business_state(agent, native_json, expected_scope):
    """Use public RunState restoration, then read its public JSON projection.

    The caller must verify ownership/integrity of the complete trusted snapshot
    and reconstruct the same Agent/tool definitions before invoking this helper.
    A successful call proves deserialization, not cross-container task progress.
    """
    from agents import RunState

    restored = await RunState.from_json(agent, native_json, strict_context=True)
    payload = restored.to_json(strict_context=True, include_tracing_api_key=False)
    return restored, read_business_state(payload, expected_scope)
