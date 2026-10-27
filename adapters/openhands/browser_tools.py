"""通过官方 MCP 公开接口把浏览器基础操作接入 OpenHands（同步执行器）。"""

from datetime import timedelta
import json
import threading
from uuid import uuid4

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from detailed_support import BROWSER_TOOLS, REQUIRED_BROWSER_TOOLS, copy_download, native_download_paths


class BrowserBridge:
    """在后台事件循环里持有一个 MCP 会话，供同步工具执行器调用。

    OpenHands 的 ToolExecutor 是同步接口，而官方 MCP 客户端是异步的。
    这里不伪造同步 API，而是把真实异步调用投递到专用事件循环，
    因此模型看到的仍是原生 MCP 工具定义与原生返回内容。
    """

    def __init__(self, options, workspace, evidence, timeout_seconds):
        self.options, self.workspace, self.evidence = options, workspace, evidence
        self.timeout = min(60, timeout_seconds)
        self.loop = None
        self.thread = None
        self.stack = None
        self.session = None
        self.definitions = {}
        self.download = None
        self.ready = threading.Event()

    def _run_loop(self):
        import asyncio
        loop = asyncio.new_event_loop()
        self.loop = loop
        asyncio.set_event_loop(loop)
        self.ready.set()
        loop.run_forever()

    def _submit(self, coroutine):
        import asyncio
        if self.loop is None:
            raise RuntimeError("浏览器 MCP 事件循环尚未启动")
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(self.timeout + 5)

    def open(self):
        self.ready = threading.Event()
        self.thread = threading.Thread(target=self._run_loop, daemon=True, name="browser-mcp")
        self.thread.start()
        # 等待后台线程真正建立事件循环，避免竞态。
        if not self.ready.wait(timeout=10):
            raise RuntimeError("浏览器 MCP 事件循环未就绪")
        return self._submit(self._open())

    async def _open(self):
        from contextlib import AsyncExitStack
        self.stack = AsyncExitStack()
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
            result = await self.session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
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
        return self.definitions

    def call(self, name, arguments):
        return self._submit(self._call(name, arguments))

    async def _call(self, name, arguments):
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
            raise RuntimeError(f"浏览器 MCP 传输或协议错误：{type(error).__name__}: {error}") from error
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
        return payload, result.isError

    def close(self):
        if self.loop is not None and not self.loop.is_closed():
            try:
                self._submit(self.stack.aclose())
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.thread is not None:
            self.thread.join(timeout=10)
