"""Project trusted HTTP JSON Schemas through ADK's documented BaseTool API."""

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
        # This projection never retries tools. The agent must inspect an unknown
        # write outcome itself rather than repeating a potentially committed write.
        try:
            response = await self.client.post("/call", json={"name": self.name, "arguments": args})
        except httpx.HTTPError:
            return {"error": {"code": "http_transport_failure", "message": "Tool transport failed; side-effect state is unknown"}}
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
        raise ValueError("Invalid trusted tool listing")
    tools, seen = [], set()
    for entry in payload["tools"]:
        name = entry.get("name")
        if not isinstance(name, str) or not name or not name.replace("_", "").isalnum() or name in seen or not isinstance(entry.get("description"), str) or not isinstance(entry.get("input_schema"), dict) or entry["input_schema"].get("type") != "object":
            raise ValueError("Invalid or duplicate trusted tool definition")
        seen.add(name)
        tools.append(HttpTool(entry, client))
    return tools
