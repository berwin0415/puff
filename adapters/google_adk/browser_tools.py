"""通过官方 MCP 公开接口把浏览器基础操作接入 ADK。

ADK 的 BaseTool.run_async 本身就是异步的，因此直接在一个
AsyncExitStack 上持有官方 MCP 会话，不需要后台事件循环或线程桥。
工具名、描述和输入结构都取自 MCP 服务，不改名也不改写 schema。
"""

from contextlib import AsyncExitStack
from datetime import timedelta
import json
from uuid import uuid4

import httpx
from google.adk.tools.base_tool import BaseTool
from google.genai import types
from mcp import ClientSession, types as mcp_types
from mcp.client.streamable_http import streamable_http_client

from detailed_support import BROWSER_TOOLS, REQUIRED_BROWSER_TOOLS, copy_download, native_download_paths


class BrowserMcpTool(BaseTool):
    def __init__(self, definition, bridge):
        super().__init__(name=definition.name, description=definition.description or definition.name)
        self.schema = definition.inputSchema or {"type": "object", "properties": {}}
        self.bridge = bridge

    def _get_declaration(self):
        return types.FunctionDeclaration(
            name=self.name, description=self.description, parameters_json_schema=self.schema,
        )

    async def run_async(self, *, args: dict, tool_context) -> dict:
        if set(args) - set(self.schema.get("properties", {})):
            return {"error": {"code": "unexpected_browser_arguments"}}
        # ADK 可能把未提供（Optional）的参数补成 null；MCP 服务按
        # JSON Schema 校验不接受 null。只移除模型未提供值，不补默认值。
        arguments = {key: value for key, value in args.items() if value is not None}
        return await self.bridge.call(self.name, arguments)


class BrowserBridge:
    def __init__(self, options, workspace, evidence, timeout_seconds):
        self.options, self.workspace, self.evidence = options, workspace, evidence
        self.timeout = min(60, timeout_seconds)
        self.stack = AsyncExitStack()
        self.session = None
        self.download = None
        self.definitions = {}

    async def open(self):
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
        self.evidence.append("mcp-events.jsonl", {
            "time": self.evidence.now(), "scope": self.evidence.scope, "phase": "initialize",
            "result": initialized.model_dump(mode="json", by_alias=True, exclude_none=True),
            "session_id": session_id(),
        })
        definitions, cursor, seen = {}, None, set()
        while True:
            result = await self.session.list_tools(params=mcp_types.PaginatedRequestParams(cursor=cursor))
            self.evidence.append("mcp-events.jsonl", {
                "time": self.evidence.now(), "scope": self.evidence.scope, "phase": "list_tools",
                "result": result.model_dump(mode="json", by_alias=True, exclude_none=True),
            })
            for tool in result.tools:
                if tool.name in definitions:
                    raise ValueError("浏览器 MCP 返回了重复的工具")
                definitions[tool.name] = tool
            cursor = result.nextCursor
            if cursor is None:
                break
            if cursor in seen:
                raise ValueError("浏览器 MCP 返回了重复的分页游标")
            seen.add(cursor)
        if not REQUIRED_BROWSER_TOOLS <= definitions.keys():
            raise ValueError("浏览器 MCP 缺少必需的基础操作工具")
        self.definitions = {name: definitions[name] for name in sorted(BROWSER_TOOLS & definitions.keys())}
        self.evidence.save("browser-tool-surface.json", {
            "scope": self.evidence.scope,
            "transport": "official_mcp_streamable_http_client",
            "sdk": "mcp==1.28.1",
            "discovered": sorted(definitions),
            "allowed": sorted(self.definitions),
            "business_http_tools": False,
            "script_tools": False,
        })
        return [BrowserMcpTool(definition, self) for definition in self.definitions.values()]

    async def call(self, name, arguments):
        call_id = uuid4().hex
        base = {"scope": self.evidence.scope, "call_id": call_id, "native_tool": name}
        self.evidence.append("mcp-events.jsonl", {
            **base, "time": self.evidence.now(), "phase": "start", "arguments": arguments,
        })
        try:
            result = await self.session.call_tool(
                name, arguments=arguments, read_timeout_seconds=timedelta(seconds=self.timeout),
            )
        except Exception as error:
            source = self.evidence.append("mcp-events.jsonl", {
                **base, "time": self.evidence.now(), "phase": "error", "status": 500,
                "error_type": type(error).__name__, "error": str(error),
            })
            self.evidence.append("browser-actions.jsonl", {
                **base, "tool": name, "phase": "finish", "status": 500, "source": source,
            })
            return {"error": {"code": "browser_mcp_transport_error",
                              "message": f"{type(error).__name__}: {error}"}}
        payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
        status = 500 if result.isError else 200
        source = self.evidence.append("mcp-events.jsonl", {
            **base, "time": self.evidence.now(), "phase": "finish", "status": status, "result": payload,
        })
        self.evidence.append("browser-actions.jsonl", {
            **base, "tool": name, "phase": "finish", "status": status, "source": source,
        })
        if not result.isError:
            for native_path in native_download_paths(payload):
                try:
                    if self.download is not None:
                        self.evidence.append("download-events.jsonl", {
                            **base, "source": source, "native_path": native_path,
                            "status": "additional_download_not_exported",
                        })
                        continue
                    self.download = copy_download(native_path, self.options["artifacts_dir"], self.workspace)
                    self.evidence.append("download-events.jsonl", {
                        **base, "source": source, "status": "copied", **self.download,
                    })
                except (OSError, ValueError) as error:
                    self.evidence.append("download-events.jsonl", {
                        **base, "source": source, "native_path": native_path,
                        "status": "missing", "error": str(error),
                    })
        # 原样交给模型：MCP 的 canonical 内容，不额外改写。
        return payload

    async def close(self):
        await self.stack.aclose()
