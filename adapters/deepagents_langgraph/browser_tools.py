"""通过固定版本官方 Python SDK 的公开 API 提供 MCP 基础操作工具。"""

from contextlib import AsyncExitStack
from datetime import timedelta
import json
from uuid import uuid4

import httpx
from langchain_core.tools import StructuredTool, ToolException
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from detailed_support import BROWSER_TOOLS, REQUIRED_BROWSER_TOOLS, copy_download, native_download_paths


class BrowserTools:
    def __init__(self, options, workspace, evidence, timeout_seconds):
        self.options, self.workspace, self.evidence = options, workspace, evidence
        self.timeout = min(60, timeout_seconds)
        self.stack = AsyncExitStack()
        self.session = None
        self.names = {}
        self.download = None
        self.denied = False

    async def load(self):
        client = await self.stack.enter_async_context(httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout), trust_env=False,
            transport=httpx.AsyncHTTPTransport(retries=0),
        ))
        read, write, session_id = await self.stack.enter_async_context(streamable_http_client(
            self.options["url"], http_client=client, terminate_on_close=True,
        ))
        self.session = await self.stack.enter_async_context(ClientSession(
            read, write, read_timeout_seconds=timedelta(seconds=self.timeout),
        ))
        initialized = await self.session.initialize()
        self.evidence.append("mcp-events.jsonl", {"time": self.evidence.now(), "scope": self.evidence.scope,
            "phase": "initialize", "result": initialized.model_dump(mode="json", by_alias=True, exclude_none=True),
            "session_id": session_id()})
        definitions = {}
        cursor = None
        seen_cursors = set()
        while True:
            result = await self.session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
            self.evidence.append("mcp-events.jsonl", {"time": self.evidence.now(), "scope": self.evidence.scope,
                "phase": "list_tools", "result": result.model_dump(mode="json", by_alias=True, exclude_none=True)})
            for tool in result.tools:
                if tool.name in definitions:
                    raise ValueError("浏览器 MCP 返回了重复的工具")
                definitions[tool.name] = tool
            cursor = result.nextCursor
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise ValueError("浏览器 MCP 返回了重复的分页游标")
            seen_cursors.add(cursor)
        if not REQUIRED_BROWSER_TOOLS <= definitions.keys():
            raise ValueError("浏览器 MCP 缺少必需的基础操作工具")
        self.evidence.save("browser-tool-surface.json", {"scope": self.evidence.scope,
            "transport": "official_mcp_streamable_http_client", "sdk": "mcp==1.28.1",
            "discovered": sorted(definitions), "allowed": sorted(BROWSER_TOOLS & definitions.keys()),
            "business_http_tools": False, "script_tools": False})
        tools = []
        for name in sorted(BROWSER_TOOLS & definitions.keys()):
            definition = definitions[name]
            self.names[name] = name
            tools.append(StructuredTool(
                name=name, description=definition.description or name,
                args_schema=definition.inputSchema, coroutine=self._forward(name),
                handle_tool_error=True,
            ))
        return tools

    def _forward(self, name):
        async def execute(**arguments):
            call_id = uuid4().hex
            base = {"scope": self.evidence.scope, "call_id": call_id, "native_tool": name}
            self.evidence.append("mcp-events.jsonl", {**base, "time": self.evidence.now(), "phase": "start", "arguments": arguments})
            try:
                result = await self.session.call_tool(name, arguments=arguments, read_timeout_seconds=timedelta(seconds=self.timeout))
            except Exception as error:
                source = self.evidence.append("mcp-events.jsonl", {**base, "time": self.evidence.now(), "phase": "error",
                    "status": 500, "error_type": type(error).__name__, "error": str(error),
                    "native_error": getattr(error, "error", None)})
                self.evidence.append("browser-actions.jsonl", {**base, "tool": name, "phase": "finish", "status": 500, "source": source})
                raise ToolException(f"浏览器 MCP 传输或协议错误：{type(error).__name__}: {error}") from error
            payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            status = 500 if result.isError else 200
            source = self.evidence.append("mcp-events.jsonl", {**base, "time": self.evidence.now(), "phase": "finish", "status": status, "result": payload})
            self.evidence.append("browser-actions.jsonl", {**base, "tool": name, "phase": "finish", "status": status, "source": source})
            if result.isError:
                raise ToolException(json.dumps(payload, ensure_ascii=False))
            for native_path in native_download_paths(payload):
                try:
                    if self.download is not None:
                        self.evidence.append("download-events.jsonl", {**base, "source": source, "native_path": native_path, "status": "additional_download_not_exported"})
                        continue
                    self.download = copy_download(native_path, self.options["artifacts_dir"], self.workspace)
                    self.evidence.append("download-events.jsonl", {**base, "source": source, "status": "copied", **self.download})
                except (OSError, ValueError) as error:
                    self.evidence.append("download-events.jsonl", {**base, "source": source, "native_path": native_path, "status": "missing", "error": str(error)})
            # 在保留的规范证据中保存完整 SDK 协议字段；
            # Agent 接收相同的序列化 CallToolResult 内容。
            return json.dumps(payload, ensure_ascii=False)

        return execute

    async def close(self):
        await self.stack.aclose()
