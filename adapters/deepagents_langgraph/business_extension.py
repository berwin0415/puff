"""Business observations in a public LangGraph state channel, without a runner.

The reducer merges completed observations so concurrent tool calls cannot replace
one another's state. Only actual tool responses can confirm a receipt. Nothing
here performs a receipt query, advances a task or replays a user prompt.
"""

from copy import deepcopy
import time


def new_business_state(scope):
    return {"schema_version": 1, "scope": deepcopy(scope), "operation_id": None,
        "receipt_confirmed": False, "operations": {}, "observations": {}}


def quantity_denial(arguments, quantity_limit):
    quantity = arguments.get("quantity")
    if type(quantity) is int and quantity > quantity_limit:
        return {"error": "Business write quantity exceeds the execution rule", "reason": "quantity_limit_exceeded",
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
    """Public state-channel reducer; complete tool observations are immutable."""
    if not left:
        left = new_business_state(right["scope"])
    if not right:
        return deepcopy(left)
    if left.get("schema_version") != 1 or right.get("schema_version") != 1 or left.get("scope") != right.get("scope"):
        raise ValueError("Business state scope/schema mismatch")
    state = new_business_state(left["scope"])
    observations = {**deepcopy(left["observations"]), **deepcopy(right["observations"])}
    for call_id in left["observations"].keys() & right["observations"].keys():
        if left["observations"][call_id] != right["observations"][call_id]:
            raise ValueError("A native tool call has conflicting business observations")
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
        raise ValueError("Native graph state lacks the trusted business channel")
    rebuilt = merge_business_state(new_business_state(expected_scope), state)
    if rebuilt != state:
        raise ValueError("Native business state projection is inconsistent with observations")
    return deepcopy(state)


def state_schema():
    """Build the documented custom DeepAgentState only inside the candidate."""
    from typing import Annotated
    from deepagents import DeepAgentState

    class BusinessDeepAgentState(DeepAgentState):
        business_state: Annotated[dict, merge_business_state]

    return BusinessDeepAgentState
