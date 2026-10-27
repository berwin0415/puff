"""仅用于 I23 的 HTTP 发出前报告；不重试、查询回执、保存或恢复。"""
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
            raise ValueError("I23 写入前绑定不能跟随链接")
    if not os.statvfs(path).f_flag & os.ST_RDONLY:
        raise ValueError("I23 写入前凭证必须使用独立的只读挂载")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(descriptor, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("写入前凭证文件无效或超出大小限制")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError("写入前凭证文件超过大小上限")
    return data


class PrewriteClient:
    @classmethod
    def from_environment(cls, endpoint, expected_scope, evidence):
        path = os.environ.get("HARNESS_I23_PREWRITE_BINDING_FILE")
        if path is None:
            return None
        if path != "/config/prewrite.json" or endpoint.rstrip("/") != "http://tools:8081":
            raise ValueError("I23 写入前检查必须使用固定的可信接口与绑定")
        binding = json.loads(_file(Path(path), 65536))
        if (not isinstance(binding, dict) or set(binding) != {"schema", "chain_id", "scope", "attempt_id", "token_file"}
                or binding["schema"] != "harness-lab-prewrite-client/1"
                or not expected_scope or binding["scope"] != expected_scope
                or expected_scope["experiment_id"] != "fault-recovery"
                or binding["chain_id"] != expected_scope["variant"]
                or binding["attempt_id"] not in {"worker-a", "worker-b"}
                or binding["token_file"] != "/config/prewrite.token"):
            raise ValueError("绑定到本次尝试的 I23 写入前配置无效")
        token = _file(Path(binding["token_file"]), 8192).decode().strip()
        if not 16 <= len(token) <= 512 or any(char.isspace() for char in token):
            raise ValueError("I23 尝试凭证无效")
        return cls(binding, token, evidence)

    def __init__(self, binding, token, evidence):
        self.binding, self.token, self.evidence = binding, token, evidence

    def event(self, phase, call_id, native_call_id, arguments, **details):
        if self.evidence is not None:
            self.evidence.append("prewrite-events.jsonl", {"time": self.evidence.now(),
                "scope": self.binding["scope"], "chain_id": self.binding["chain_id"],
                "attempt_id": self.binding["attempt_id"], "phase": phase,
                "bridge_call_id": call_id, "native_call_id": native_call_id,
                "native_call_id_origin": "ToolRuntime.tool_call_id" if native_call_id else "缺失；仅有桥接器生成的 UUID",
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
            # 原始调用栈必须等此处返回许可后才能 POST /call；
            # 目标报告保持阻塞，直到进程被杀死或请求被拒绝。
            reply = await client.post("/experiment/prewrite", json=report, headers=headers)
            if reply.status_code != 200:
                raise PrewriteDenied("实验写入前报告被拒绝")
            value = reply.json()
            if (not isinstance(value, dict) or set(value) != {"permit"} or not isinstance(value["permit"], str)
                    or not 16 <= len(value["permit"]) <= 512 or any(char.isspace() for char in value["permit"])):
                raise PrewriteDenied("实验写入前许可无效")
        except (httpx.HTTPError, ValueError, PrewriteDenied) as error:
            self.event("report_not_admitted", call_id, native_call_id, arguments,
                actual_call_dispatched=False, error_type=type(error).__name__)
            raise PrewriteDenied("实验门控未放行业务 HTTP 请求") from None
        self.event("report_admitted", call_id, native_call_id, arguments, actual_call_dispatched=False)
        return {**headers, CALL_HEADER: call_id, PERMIT_HEADER: value["permit"]}
