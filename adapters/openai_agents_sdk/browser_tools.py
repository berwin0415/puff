"""Original MCP schemas exposed as public SDK FunctionTools, no task script."""
from contextlib import AsyncExitStack
import json
from uuid import uuid4

from agents import FunctionTool
import httpx2
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client

from detailed_support import BROWSER_TOOLS, REQUIRED_BROWSER_TOOLS, copy_download, native_download_paths


class BrowserTools:
    def __init__(self, options, workspace, evidence, timeout_seconds, identity):
        self.options, self.workspace, self.evidence, self.identity = options, workspace, evidence, identity
        self.timeout = min(60, timeout_seconds)
        self.stack = AsyncExitStack()
        self.session, self.download = None, None
        self.denied = False

    async def load(self):
        client = await self.stack.enter_async_context(httpx2.AsyncClient(
            timeout=httpx2.Timeout(self.timeout), trust_env=False, follow_redirects=False,
            transport=httpx2.AsyncHTTPTransport(retries=0)))
        read, write = await self.stack.enter_async_context(streamable_http_client(
            self.options["url"], http_client=client, terminate_on_close=True))
        # mcp 2.3.0 (already in the frozen graph) takes float seconds, not the
        # timedelta used by MCP v1. Use only its public ClientSession methods.
        self.session = await self.stack.enter_async_context(ClientSession(
            read, write, read_timeout_seconds=self.timeout))
        initialized = await self.session.initialize()
        self.evidence.append("mcp-events.jsonl", {"scope": self.evidence.scope, "time": self.evidence.now(),
            "phase": "initialize", "result": initialized.model_dump(mode="json", by_alias=True, exclude_none=True)})
        definitions, cursor, seen_cursors = {}, None, set()
        while True:
            result = await self.session.list_tools(params=types.PaginatedRequestParams(cursor=cursor))
            self.evidence.append("mcp-events.jsonl", {"scope": self.evidence.scope, "time": self.evidence.now(),
                "phase": "list_tools", "result": result.model_dump(mode="json", by_alias=True, exclude_none=True)})
            for tool in result.tools:
                if tool.name in definitions:
                    raise ValueError("MCP listed a duplicate browser tool")
                definitions[tool.name] = tool
            cursor = result.next_cursor
            if cursor is None:
                break
            if cursor in seen_cursors:
                raise ValueError("MCP repeated a pagination cursor")
            seen_cursors.add(cursor)
        if not REQUIRED_BROWSER_TOOLS <= definitions.keys():
            raise ValueError("MCP is missing required browser primitives")
        self.evidence.save("browser-tool-surface.json", {"scope": self.evidence.scope,
            "transport": "official_mcp_ClientSession_streamable_http", "sdk": "mcp==2.3.0",
            "discovered": sorted(definitions), "allowed": sorted(BROWSER_TOOLS & definitions.keys()),
            "business_http_tools": False, "script_tools": False})
        tools = []
        for name in sorted(BROWSER_TOOLS & definitions.keys()):
            definition = definitions[name]
            schema = definition.input_schema
            if not isinstance(schema, dict) or schema.get("type") != "object":
                raise ValueError("Expected an original MCP object schema")
            tools.append(FunctionTool(name=name, description=definition.description or name,
                params_json_schema=schema, on_invoke_tool=self._forward(name),
                strict_json_schema=False, needs_approval=False))
        return tools

    def _forward(self, name):
        async def invoke(context, raw_arguments):
            if context.context != self.identity:
                raise ValueError("Browser tool invocation belongs to a different trusted run")
            arguments = json.loads(raw_arguments)
            if not isinstance(arguments, dict):
                raise ValueError("Expected original object arguments")
            call_id = uuid4().hex
            base = {"scope": self.evidence.scope, "call_id": call_id, "native_tool": name}
            self.evidence.append("mcp-events.jsonl", {**base, "time": self.evidence.now(), "phase": "start", "arguments": arguments})
            try:
                result = await self.session.call_tool(name, arguments=arguments, read_timeout_seconds=self.timeout)
            except Exception as error:
                source = self.evidence.append("mcp-events.jsonl", {**base, "time": self.evidence.now(),
                    "phase": "error", "status": 500, "error_type": type(error).__name__, "error": str(error)})
                self.evidence.append("browser-actions.jsonl", {**base, "tool": name, "phase": "finish", "status": 500, "source": source})
                # No application retry: the Agent sees failure and chooses its
                # next action. The original error is retained with provenance.
                return json.dumps({"ok": False, "error_type": type(error).__name__, "error": str(error)})
            payload = result.model_dump(mode="json", by_alias=True, exclude_none=True)
            status = 500 if result.is_error else 200
            source = self.evidence.append("mcp-events.jsonl", {**base, "time": self.evidence.now(),
                "phase": "finish", "status": status, "result": payload})
            self.evidence.append("browser-actions.jsonl", {**base, "tool": name, "phase": "finish", "status": status, "source": source})
            if not result.is_error:
                for native_path in native_download_paths(payload):
                    try:
                        if self.download is not None:
                            self.evidence.append("download-events.jsonl", {**base, "source": source,
                                "native_path": native_path, "status": "additional_download_not_exported"})
                            continue
                        self.download = copy_download(native_path, self.options["artifacts_dir"], self.workspace)
                        self.evidence.append("download-events.jsonl", {**base, "source": source, "status": "copied", **self.download})
                    except (OSError, ValueError) as error:
                        self.evidence.append("download-events.jsonl", {**base, "source": source,
                            "native_path": native_path, "status": "missing", "error": str(error)})
            return json.dumps(payload, ensure_ascii=False)

        return invoke

    async def close(self):
        await self.stack.aclose()
