"""通过 ADK 文档中的 BaseTool API 投影可信 HTTP JSON 结构。"""

import httpx
from google.adk.tools.base_tool import BaseTool
from google.genai import types


class HttpTool(BaseTool):
    def __init__(self, definition: dict, client: httpx.AsyncClient):
        super().__init__(name=definition["name"], description=definition["description"])
        self.schema = definition["input_schema"]
        self.client = client

    def _get_declaration(self):
        return types.FunctionDeclaration(
            name=self.name,
            description=self.description,
            parameters_json_schema=self.schema,
        )

    async def run_async(self, *, args: dict, tool_context) -> dict:
        # 此投影不重试工具；写入结果未知时，Agent 必须自行检查，
        # 避免重复执行可能已提交的写入。
        try:
            response = await self.client.post("/call", json={"name": self.name, "arguments": args})
        except httpx.HTTPError:
            return {"error": {"code": "http_transport_failure", "message": "工具传输失败；副作用状态未知"}}
        try:
            body = response.json()
        except ValueError:
            return {"error": {"code": "invalid_tool_response", "http_status": response.status_code}}
        if response.is_error:
            return {"error": {"http_status": response.status_code, "service_error": body}}
        return body


async def load_tools(client: httpx.AsyncClient) -> list[HttpTool]:
    response = await client.get("/tools")
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not isinstance(payload.get("tools"), list) or not payload["tools"]:
        raise ValueError("可信工具清单无效")
    tools, seen = [], set()
    for entry in payload["tools"]:
        name = entry.get("name")
        if not isinstance(name, str) or not name or not name.replace("_", "").isalnum() or name in seen or not isinstance(entry.get("description"), str) or not isinstance(entry.get("input_schema"), dict) or entry["input_schema"].get("type") != "object":
            raise ValueError("可信工具定义无效或重复")
        seen.add(name)
        tools.append(HttpTool(entry, client))
    return tools
