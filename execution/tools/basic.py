"""Small deterministic tools shared by candidate adapters.

These helpers are intentionally independent of any Agent SDK.  An adapter asks
the execution service to run them, making tool behavior comparable across
candidates during T1 and T5.
"""

from __future__ import annotations

import argparse
import ast
import json
import operator
from pathlib import Path
from typing import Any
from urllib import error, request


_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
    ast.USub: operator.neg,
    ast.UAdd: operator.pos,
}


def _eval(node: ast.AST) -> int | float:
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPERATORS:
        return _OPERATORS[type(node.op)](_eval(node.operand))
    if isinstance(node, ast.BinOp) and type(node.op) in _OPERATORS:
        left, right = _eval(node.left), _eval(node.right)
        # Keep accidental resource exhaustion out of the local harness.
        if isinstance(node.op, ast.Pow) and abs(right) > 12:
            raise ValueError("exponent is too large")
        return _OPERATORS[type(node.op)](left, right)
    raise ValueError("expression contains an unsupported operation")


def safe_calculate(expression: str) -> int | float:
    """Evaluate a small arithmetic expression without executing code."""

    if not isinstance(expression, str) or not expression.strip():
        raise ValueError("expression must be a non-empty string")
    return _eval(ast.parse(expression, mode="eval"))


def write_workspace_file(workspace: str | Path, relative_path: str, content: str) -> str:
    """Write a UTF-8 file below *workspace* and reject path traversal."""

    root = Path(workspace).resolve()
    target = (root / relative_path).resolve()
    if target != root and root not in target.parents:
        raise ValueError("relative_path escapes workspace")
    if not isinstance(content, str):
        raise ValueError("content must be a string")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return str(target.relative_to(root))


def http_request(
    url: str,
    method: str = "GET",
    payload: Any = None,
    idempotency_key: str | None = None,
    timeout: float = 10,
) -> dict[str, Any]:
    """Call a fixture endpoint and return status plus a decoded response."""

    data = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key
    req = request.Request(url, data=data, headers=headers, method=method.upper())
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
            return {"status_code": response.status, "body": json.loads(raw) if raw else None}
    except error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        body = json.loads(raw) if raw else None
        return {"status_code": exc.code, "body": body}


def execute(name: str, arguments: dict[str, Any], *, workspace: str | Path = ".") -> Any:
    """Dispatch a named baseline tool with JSON-compatible arguments."""

    if name == "calculate":
        return safe_calculate(str(arguments.get("expression", "")))
    if name == "write_file":
        return write_workspace_file(workspace, str(arguments.get("path", "")), str(arguments.get("content", "")))
    if name == "http_request":
        return http_request(
            str(arguments.get("url", "")),
            method=str(arguments.get("method", "GET")),
            payload=arguments.get("payload"),
            idempotency_key=arguments.get("idempotency_key"),
            timeout=float(arguments.get("timeout", 10)),
        )
    raise KeyError(f"unknown execution tool: {name}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tool", required=True, choices=("calculate", "write_file", "http_request"))
    parser.add_argument("--arguments", required=True, help="JSON object containing tool arguments")
    parser.add_argument("--workspace", default=".")
    args = parser.parse_args()
    result = execute(args.tool, json.loads(args.arguments), workspace=args.workspace)
    print(json.dumps({"ok": True, "result": result}, ensure_ascii=False))


if __name__ == "__main__":
    main()
