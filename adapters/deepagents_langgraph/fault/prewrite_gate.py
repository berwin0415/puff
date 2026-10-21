"""I23-only BEFORE-HTTP report; no retry, receipt query, save or recovery."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat

import httpx


TOKEN_HEADER = "X-Harness-Prewrite-Token"
PERMIT_HEADER = "X-Harness-Prewrite-Permit"
CALL_HEADER = "X-Harness-Prewrite-Call-Id"


class PrewriteDenied(RuntimeError):
    pass


def _file(path, limit):
    current = Path("/")
    for part in path.parts[1:]:
        current /= part
        if current.is_symlink():
            raise ValueError("I23 prewrite binding cannot follow links")
    if not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise ValueError("I23 prewrite credentials require separate read-only mounts")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Invalid bounded prewrite credential file")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Prewrite credential file exceeded limit")
    return data


class PrewriteClient:
    @classmethod
    def from_environment(cls, endpoint, expected_scope, evidence):
        path = os.environ.get("HARNESS_I23_PREWRITE_BINDING_FILE")
        if path is None:
            return None
        if path != "/config/prewrite.json" or endpoint.rstrip("/") != "http://tools:8081":
            raise ValueError("I23 prewrite requires the fixed trusted endpoint and binding")
        binding = json.loads(_file(Path(path), 65536))
        if (not isinstance(binding, dict) or set(binding) != {"schema", "chain_id", "scope", "attempt_id", "token_file"}
                or binding["schema"] != "harness-lab-prewrite-client/1"
                or not expected_scope or binding["scope"] != expected_scope
                or expected_scope["experiment_id"] != "fault-recovery"
                or binding["chain_id"] != expected_scope["variant"]
                or binding["attempt_id"] not in {"worker-a", "worker-b"}
                or binding["token_file"] != "/config/prewrite.token"):
            raise ValueError("Invalid attempt-bound I23 prewrite binding")
        token = _file(Path(binding["token_file"]), 8192).decode().strip()
        if not 16 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise ValueError("Invalid I23 attempt credential")
        return cls(binding, token, evidence)

    def __init__(self, binding, token, evidence):
        self.binding, self.token, self.evidence = binding, token, evidence

    def event(self, phase, call_id, native_call_id, arguments, **details):
        if self.evidence is not None:
            self.evidence.append("prewrite-events.jsonl", {"time": self.evidence.now(),
                "scope": self.binding["scope"], "chain_id": self.binding["chain_id"],
                "attempt_id": self.binding["attempt_id"], "phase": phase,
                "bridge_call_id": call_id, "native_call_id": native_call_id,
                "native_call_id_origin": "ToolRuntime.tool_call_id" if native_call_id else "absent; bridge UUID only",
                "arguments_sha256": hashlib.sha256(json.dumps(arguments, sort_keys=True,
                    ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()).hexdigest(), **details})

    async def before(self, client, tool, arguments, call_id, *, native_call_id=None):
        headers = {TOKEN_HEADER: self.token}
        if tool != "write":
            return headers
        report = {"chain_id": self.binding["chain_id"], "scope": self.binding["scope"],
            "attempt_id": self.binding["attempt_id"], "bridge_call_id": call_id,
            "native_call_id": native_call_id, "tool": tool, "arguments": arguments}
        self.event("before_report", call_id, native_call_id, arguments, actual_call_dispatched=False)
        try:
            # The same original stack cannot POST /call until this returns a
            # permit. Target reports remain blocked until killed or refused.
            reply = await client.post("/experiment/prewrite", json=report, headers=headers)
            if reply.status_code != 200:
                raise PrewriteDenied("Experiment prewrite report refused")
            value = reply.json()
            if (not isinstance(value, dict) or set(value) != {"permit"} or not isinstance(value["permit"], str)
                    or not 16 <= len(value["permit"]) <= 512 or any(char.isspace() for char in value["permit"])):
                raise PrewriteDenied("Experiment prewrite permit invalid")
        except (httpx.HTTPError, ValueError, PrewriteDenied) as error:
            self.event("report_not_admitted", call_id, native_call_id, arguments,
                actual_call_dispatched=False, error_type=type(error).__name__)
            raise PrewriteDenied("Experiment gate did not admit the business HTTP request") from None
        self.event("report_admitted", call_id, native_call_id, arguments, actual_call_dispatched=False)
        return {**headers, CALL_HEADER: call_id, PERMIT_HEADER: value["permit"]}
